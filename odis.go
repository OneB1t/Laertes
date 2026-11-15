package main

import (
	"bytes"
	"cmp"
	_ "embed"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"
)

// The bridge macro ships inside laertes.exe and is installed into ODIS's macro folder, so the
// bridge always matches this version of Laertes.
//
//go:embed mcpserver.py
var bridgeMacro []byte

//go:embed mcpserver_handlers.py
var bridgeHandlers []byte

// odisProduct selects which ODIS installation Laertes drives: "OE" (Offboard Engineering) or "OS"
// (Offboard Service). Both install an OffboardDiagLauncher.exe under C:\Program Files\<product> with
// data under C:\ProgramData\<product>. It is chosen in the top bar (saved as the odisProduct setting)
// and applied by applyODISProduct, which recomputes every product-dependent path.
//
// Use "OS" for guided functions such as component protection: ODIS-E does not execute the guided-
// function step classes (engine.log fills with "no access to class ...$Step_N", so the steps -- the
// reads, routines and the writes -- are skipped), whereas ODIS-S is the service product that runs them.
var odisProduct = "OE"

// odisDir is ODIS's data folder (macros, configuration); odisInstallDir holds its launcher. These and
// the offline-VCI/stub paths under odisDir are all set by applyODISProduct from odisProduct.
var (
	odisDir        string
	odisInstallDir string
)

func init() { applyODISProduct(odisProduct) }

// applyODISProduct points odisDir, odisInstallDir and every path derived from them at ODIS product p
// ("OE" or "OS"; anything else falls back to "OE"). Those paths are read when a start, a CAN-log poll
// or a GSB request actually runs, so a change takes effect on the next bridge start: switch while the
// bridge is stopped, since a running ODIS keeps using the paths it was started with.
func applyODISProduct(p string) {
	if p != "OS" {
		p = "OE"
	}
	odisProduct = p
	odisDir = filepath.Join(envOr("ProgramData", `C:\ProgramData`), p)
	odisInstallDir = filepath.Join(envOr("ProgramFiles", `C:\Program Files`), p)
	offlineVCIPath = filepath.Join(odisDir, "laertes", "laertes_vci32.dll")
	offlineConfPath = filepath.Join(odisDir, "laertes", "offline.conf")
	wakeFlagPath = filepath.Join(odisDir, "laertes", "wake.on")
	offlineCANLogPath = filepath.Join(odisDir, "laertes", "vci-can.log")
	gsbLogPath = filepath.Join(odisDir, "laertes", "gsb.log")
}

// odisService is ODIS's automation web service, served when ODIS is started without its window
// (see odisWebServiceINI). Its executeMacro runs the bridge macro.
const odisService = "http://127.0.0.1:8081/OdisAutomationService"

func envOr(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

// installBridge writes the bridge macros into dir when they are missing or differ from the ones
// in laertes.exe. Each file is replaced in one step, because the bridge reloads
// mcpserver_handlers.py as soon as it changes and must never read half a file.
func installBridge(dir string) error {
	if _, err := os.Stat(filepath.Dir(dir)); err != nil {
		return fmt.Errorf("ODIS is not installed here (%s not found)", filepath.Dir(dir))
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	for name, data := range map[string][]byte{"mcpserver.py": bridgeMacro, "mcpserver_handlers.py": bridgeHandlers} {
		path := filepath.Join(dir, name)
		if old, err := os.ReadFile(path); err == nil && bytes.Equal(old, data) {
			continue
		}
		tmp := path + ".laertes-tmp"
		if err := os.WriteFile(tmp, data, 0o644); err != nil {
			return err
		}
		if err := os.Rename(tmp, path); err != nil {
			os.Remove(tmp)
			return err
		}
		log.Printf("installed %s", path)
	}
	return nil
}

// odisWebServiceINI is the extra ODIS configuration that starts ODIS without its window, serving
// the automation web service on odisService's port.
const odisWebServiceINI = `# Written by Laertes: starts ODIS without its window as the automation web service,
# so Laertes can run the bridge macro (mcpserver.py) in it.
de.volkswagen.odis.vaudas.vehiclefunction.automation.webservice.enabled=true
de.volkswagen.odis.vaudas.vehiclefunction.automation.webservice.port=8081
# Jython 2.7 cannot start a macro when the time zone's short name is not Latin-1 (Czech "SEČ"):
# ODIS's macro wrapper imports time first. An English default locale names it "CET".
osgi.nl=en_US
# ODIS-S opens the vehicle-identification dialog at the start of each session (config.ini sets
# always_show_identification_dialog=true). Laertes loads the project directly, so skip it to start
# faster; ODIS-E does not define the key and ignores it.
de.volkswagen.odis.vaudas.ui.diag.always_show_identification_dialog=false
eof=eof
`

// odisStarter runs the bridge macro without the user opening ODIS: it starts ODIS in the
// background as a web service, loads the vehicle project and calls executeMacro. ODIS's own
// window cannot run a macro from outside, so while ODIS is open with its window the user starts
// the macro there.
type odisStarter struct {
	bridgeAddr string
	project    func() string // the last vehicle project used, for when the car cannot be identified
	vin        func() string // the simulation VIN for offline mode (nil or "" keeps the VCI default)
	wake       func() wakeConfig // the clamp 15 wake-up frame (nil: none)
	qemu       func() qemuConfig // the virtual VCI's connection to a QEMU gateway (nil: simulate)
	product    func() string     // the chosen ODIS product ("OE"/"OS"); applied before each start

	mu       sync.Mutex
	state    string // "": not tried, "starting", "running", "error"
	message  string
	launched bool // ODIS was started here and is closed again when the bridge ends
	since    time.Time     // when the running start began (zero when none runs)
	took     time.Duration // how long the last start took until the bridge listened
}

type odisStatus struct {
	State    string  `json:"state"`
	Message  string  `json:"message"`
	Launched bool    `json:"launched"`
	VCI      string  `json:"vci"`     // the VCI ODIS is set to use (selectedVCI)
	Elapsed  float64 `json:"elapsed"` // seconds since the running start began, for the page's timer
	Took     float64 `json:"took"`    // seconds the last start took
	Wake     bool    `json:"wake"`     // the wake-up frame can be switched (ignitionState)
	Ignition bool    `json:"ignition"` // the wake-up frame is switched on
	Product  string  `json:"product"`  // the ODIS installation in use: "OE" or "OS"
}

func (o *odisStarter) wakeFrame() wakeConfig {
	if o.wake == nil {
		return wakeConfig{}
	}
	return o.wake()
}

func (o *odisStarter) status() odisStatus {
	o.mu.Lock()
	defer o.mu.Unlock()
	st := odisStatus{State: o.state, Message: o.message, Launched: o.launched, VCI: selectedVCI(), Took: o.took.Seconds(), Product: odisProduct}
	st.Wake, st.Ignition = ignitionState()
	if !o.since.IsZero() {
		st.Elapsed = time.Since(o.since).Seconds()
	}
	return st
}

// set reports a step of the start, logged with the time since the start began; "running" and
// "error" end the start.
func (o *odisStarter) set(state, format string, args ...any) {
	o.mu.Lock()
	o.state, o.message = state, fmt.Sprintf(format, args...)
	msg, elapsed := o.message, time.Duration(0)
	if !o.since.IsZero() {
		elapsed = time.Since(o.since).Round(100 * time.Millisecond)
		if state != "starting" {
			o.since = time.Time{}
			if state == "running" {
				o.took = elapsed
			}
		}
	}
	o.mu.Unlock()
	if elapsed > 0 {
		log.Printf("ODIS bridge start [%v]: %s", elapsed, msg)
	} else {
		log.Printf("ODIS bridge start: %s", msg)
	}
}

// start begins starting the bridge in the background with the given vehicle project ("" to
// identify the car by its VIN). Offline, ODIS uses the offline VCI (offlinevci.go) and loads
// project without a car or interface. It does nothing while a start is already running.
func (o *odisStarter) start(project string, offline bool) {
	if o.product != nil {
		applyODISProduct(o.product()) // honour a top-bar product change made since the last start
	}
	o.mu.Lock()
	if o.state == "starting" {
		o.mu.Unlock()
		return
	}
	o.state, o.message = "starting", "Starting the ODIS bridge…"
	o.since = time.Now()
	o.mu.Unlock()
	go func() {
		err := o.run(project, offline)
		if err == nil {
			return
		}
		// An ODIS without a window must not stay behind unseen, holding the VCI.
		o.mu.Lock()
		launched := o.launched
		o.launched = false
		o.mu.Unlock()
		if launched {
			soapCall("exit", "", "")
			err = fmt.Errorf("%w (the background ODIS was closed again)", err)
		}
		o.set("error", "%v", err)
	}()
}

func (o *odisStarter) run(project string, offline bool) error {
	if reachable(o.bridgeAddr) {
		o.set("running", "The ODIS bridge is running.")
		return nil
	}
	if err := bridgePortsFree(o.bridgeAddr); err != nil {
		return err
	}
	macro := filepath.Join(odisDir, "macros", "mcpserver.py")
	if err := installBridge(filepath.Dir(macro)); err != nil {
		return fmt.Errorf("cannot install the bridge macro: %w", err)
	}
	if !odisServing() {
		if odisRunning() {
			return errors.New("ODIS is open with its window, where Laertes cannot start a macro: run mcpserver.py from ODIS's Macros function, or close ODIS and press Start bridge")
		}
		if offline {
			if project == "" {
				return errors.New("offline mode needs a vehicle project: choose one in Settings › ODIS bridge")
			}
			o.set("starting", "Installing the offline VCI…")
			if err := installOfflineVCI(); err != nil {
				return err
			}
			conf := offlineConfig{Wake: o.wakeFrame()}
			if o.qemu != nil {
				conf.QEMU = o.qemu()
			}
			if conf.QEMU.On {
				log.Printf("virtual VCI: connected to the QEMU gateway at %s, CAN%d", conf.QEMU.Addr, conf.QEMU.Bus)
			}
			if conf.Wake.On {
				setIgnition(true) // a session with the wake-up starts with the ignition on
			}
			if o.vin != nil {
				conf.VIN = o.vin()
			}
			if err := writeOfflineConfig(conf); err != nil {
				return fmt.Errorf("write the offline VCI configuration: %w", err)
			}
			if err := useOfflineVCI(); err != nil {
				return fmt.Errorf("select the offline VCI for ODIS: %w", err)
			}
		} else if wake := o.wakeFrame(); wake.On {
			// ODIS uses Laertes's VCI DLL, which forwards to the user's own VCI and sends the
			// wake-up frame next to ODIS's traffic.
			o.set("starting", "Setting up the clamp 15 wake-up on your VCI…")
			name, lib, err := userJ2534Driver()
			if err != nil {
				return err
			}
			log.Printf("clamp 15 wake-up: forwarding to %s (%s), frame %s#%s every %d ms", name, lib, wake.ID, wake.Data, wake.Ms)
			if err := installOfflineVCI(); err != nil {
				return err
			}
			setIgnition(true) // a session with the wake-up starts with the ignition on
			if err := writeOfflineConfig(offlineConfig{Wake: wake, Passthru: lib, PassthruName: name}); err != nil {
				return fmt.Errorf("write the VCI configuration: %w", err)
			}
			if err := useOfflineVCI(); err != nil {
				return fmt.Errorf("select Laertes's VCI for ODIS: %w", err)
			}
		} else {
			restoreVCI()
		}
		if err := o.launch(); err != nil {
			return err
		}
	}
	log.Printf("ODIS uses VCI: %s", cmp.Or(selectedVCI(), "none selected"))
	o.set("starting", "Starting the bridge macro in ODIS…")
	err := o.execMacro(macro)
	if err == nil {
		return nil
	}
	// ODIS runs a macro only with a vehicle project loaded.
	if err = o.loadProject(project); err != nil {
		return err
	}
	o.set("starting", "Starting the bridge macro in ODIS…")
	return o.execMacro(macro)
}

// launch starts ODIS without its window and waits until its web service answers.
func (o *odisStarter) launch() error {
	exe := filepath.Join(odisInstallDir, "OffboardDiagLauncher.exe")
	if _, err := os.Stat(exe); err != nil {
		return fmt.Errorf("ODIS not found (%s)", exe)
	}
	ini := filepath.Join(odisDir, "configuration", "laertes-webservice.ini")
	if err := os.WriteFile(ini, []byte(odisWebServiceINI), 0o644); err != nil {
		return err
	}
	o.set("starting", "Starting ODIS in the background…")
	cmd := exec.Command(exe, "-configuration", ini)
	cmd.Dir = odisInstallDir
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start ODIS: %w", err)
	}
	o.mu.Lock()
	o.launched = true
	o.mu.Unlock()
	exited := make(chan error, 1)
	go func() {
		err := cmd.Wait()
		restoreVCI() // after an offline session, ODIS's own VCI choice applies again
		exited <- err
	}()
	for deadline := time.Now().Add(5 * time.Minute); time.Now().Before(deadline); time.Sleep(time.Second) {
		select {
		case err := <-exited:
			o.mu.Lock()
			o.launched = false
			o.mu.Unlock()
			return fmt.Errorf("ODIS ended while starting (%v); see %s", err, filepath.Join(odisDir, "log", "engine.log"))
		default:
		}
		if odisServing() {
			return nil
		}
	}
	return errors.New("ODIS did not start its web service within 5 minutes")
}

// loadProject loads the vehicle project: project when given, else identified from the car's VIN
// (ODIS Service, then Engineering project tables), else the last project used.
func (o *odisStarter) loadProject(project string) error {
	if project != "" {
		o.set("starting", "Loading vehicle project %s…", project)
		_, err := soapCall("setVehicleProject", "projectName", project)
		if err != nil {
			return fmt.Errorf("vehicle project %s: %w", project, err)
		}
		return nil
	}
	o.set("starting", "Identifying the car (reading its VIN)…")
	var errs []string
	addErr := func(prefix string, err error) {
		e := prefix + strings.TrimRight(err.Error(), ". ")
		if !slices.Contains(errs, e) {
			errs = append(errs, e)
		}
	}
	for _, mode := range []string{"SERVICE", "ENGINEERING"} {
		_, err := soapCall("setVehicleProjectAutomatic", "projectMode", mode)
		if err == nil {
			return nil
		}
		addErr("", err)
	}
	if last := o.project(); last != "" {
		o.set("starting", "Loading the last vehicle project, %s…", last)
		_, err := soapCall("setVehicleProject", "projectName", last)
		if err == nil {
			return nil
		}
		addErr(last+": ", err)
	}
	return fmt.Errorf("no vehicle project could be loaded (is the VCI connected to the car, ignition on?): %s. Enter the vehicle project in Settings › ODIS bridge and press Start bridge", strings.Join(errs, "; "))
}

// execMacro runs the bridge macro and returns once the bridge listens, or with ODIS's error. The
// call to executeMacro lasts as long as the macro runs; when it ends (Stop bridge), ODIS is closed
// again if it was started here.
func (o *odisStarter) execMacro(macro string) error {
	done := make(chan error, 1)
	go func() {
		_, err := soapCall("executeMacro", "macroName", macro)
		done <- err
		o.mu.Lock()
		launched, up := o.launched, o.state == "running"
		o.mu.Unlock()
		if !up {
			return
		}
		if launched {
			soapCall("exit", "", "")
			o.mu.Lock()
			o.launched = false
			o.mu.Unlock()
		}
		o.set("", "The bridge macro ended.")
	}()
	for deadline := time.Now().Add(time.Minute); time.Now().Before(deadline); time.Sleep(500 * time.Millisecond) {
		select {
		case err := <-done:
			if err == nil {
				err = errors.New("the bridge macro ended at once; see the ODIS macro console log")
			}
			return err
		default:
		}
		if reachable(o.bridgeAddr) {
			o.set("running", "The ODIS bridge is running.")
			return nil
		}
	}
	return errors.New("the bridge macro did not start listening within a minute")
}

// odisProjects lists the vehicle projects installed for ODIS (the folders of its MCD projects),
// for choosing one in offline mode.
func odisProjects() []string {
	var names []string
	for _, dir := range []string{"MCD-Projects", "MCD-Projects-E"} {
		entries, _ := os.ReadDir(filepath.Join(odisDir, dir, "VWMCD"))
		for _, e := range entries {
			if e.IsDir() && !slices.Contains(names, e.Name()) {
				names = append(names, e.Name())
			}
		}
	}
	slices.Sort(names)
	return names
}

// bridgePortsFree checks that the macro can listen on the bridge port and the development port
// (the next one, DEV_PORT in mcpserver.py). Windows reserves port ranges for Hyper-V, WSL and
// Docker that change with each boot; inside one the macro fails with "Address already in use".
func bridgePortsFree(addr string) error {
	host, portStr, err := net.SplitHostPort(addr)
	if err != nil {
		return nil
	}
	port, err := net.LookupPort("tcp", portStr)
	if err != nil || !(host == "127.0.0.1" || host == "localhost") {
		return nil // a bridge elsewhere is not ours to check
	}
	for _, p := range []int{port, port + 1} {
		l, err := net.Listen("tcp", net.JoinHostPort(host, fmt.Sprint(p)))
		if err != nil {
			return fmt.Errorf("the bridge cannot listen on port %d (%v). Windows has probably reserved it for Hyper-V/WSL/Docker: see `netsh interface ipv4 show excludedportrange protocol=tcp`. As administrator, run `net stop winnat` and `net start winnat` (or reboot) to free it", p, err)
		}
		l.Close()
	}
	return nil
}

func reachable(addr string) bool {
	c, err := net.DialTimeout("tcp", addr, time.Second)
	if err == nil {
		c.Close()
	}
	return err == nil
}

func odisServing() bool {
	c := http.Client{Timeout: 3 * time.Second}
	res, err := c.Get(odisService + "?wsdl")
	if err != nil {
		return false
	}
	res.Body.Close()
	return res.StatusCode == http.StatusOK
}

// odisRunning reports whether ODIS runs (with its window, when odisServing is false).
func odisRunning() bool {
	out, _ := exec.Command("tasklist", "/FI", "IMAGENAME eq OffboardDiagLauncher.exe", "/NH").Output()
	return bytes.Contains(bytes.ToLower(out), []byte("offboarddiaglauncher.exe"))
}

// soapCall calls one operation of ODIS's automation web service with at most one string argument
// and returns the response body; a SOAP fault is returned as its message.
func soapCall(op, arg, value string) ([]byte, error) {
	var body bytes.Buffer
	body.WriteString(`<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>`)
	fmt.Fprintf(&body, `<t:%s xmlns:t="http://automation.vehiclefunction.vaudas.odis.volkswagen.de">`, op)
	if arg != "" {
		fmt.Fprintf(&body, "<%s>", arg)
		xml.EscapeText(&body, []byte(value))
		fmt.Fprintf(&body, "</%s>", arg)
	}
	fmt.Fprintf(&body, "</t:%s></s:Body></s:Envelope>", op)
	req, _ := http.NewRequest(http.MethodPost, odisService, &body)
	req.Header.Set("Content-Type", "text/xml; charset=utf-8")
	req.Header.Set("SOAPAction", `""`)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("ODIS web service: %w", err)
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, err
	}
	if msg := soapFault(data); msg != "" {
		return nil, errors.New(msg)
	}
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ODIS web service: HTTP %d", res.StatusCode)
	}
	return data, nil
}

// soapFault returns the faultstring of a SOAP fault response, "" for any other response.
func soapFault(data []byte) string {
	var env struct {
		Fault *struct {
			String string `xml:"faultstring"`
		} `xml:"Body>Fault"`
	}
	if xml.Unmarshal(data, &env) != nil || env.Fault == nil {
		return ""
	}
	return strings.Join(strings.Fields(env.Fault.String), " ")
}

// settingValue reads one value from the page's settings (a JSON object), nil when unset.
func settingValue(raw json.RawMessage, key string) any {
	var m map[string]any
	json.Unmarshal(raw, &m)
	return m[key]
}
