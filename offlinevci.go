package main

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// The offline VCI (offlinevci/offlinevci.c): a J2534 device without hardware, so ODIS loads a
// vehicle project and runs the bridge macro with no interface connected.
//
//go:embed offlinevci/laertes_vci32.dll
var offlineVCIDLL []byte

const (
	offlineVCIName = "Laertes-Offline" // the device name ODIS shows and stores in vci.properties
	offlineVCIKey  = `SOFTWARE\WOW6432Node\PassThruSupport.04.04\Laertes - Offline VCI`
)

var offlineVCIPath string // set by applyODISProduct: <odisDir>\laertes\laertes_vci32.dll

// offlineConfPath is the simulation configuration the offline VCI reads (offlinevci.c loadConfig),
// written next to the DLL so the DLL finds it in its own folder.
var offlineConfPath string // set by applyODISProduct: <odisDir>\laertes\offline.conf

// wakeFlagPath switches the wake-up frame (offlinevci.c wakeLoop): sent while this file exists.
var wakeFlagPath string // set by applyODISProduct: <odisDir>\laertes\wake.on

// setIgnition switches the periodic wake-up frame on or off, live (the Ignition button).
func setIgnition(on bool) error {
	if !on {
		if err := os.Remove(wakeFlagPath); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(wakeFlagPath), 0o755); err != nil {
		return err
	}
	return os.WriteFile(wakeFlagPath, []byte("Written by Laertes: the VCI sends the clamp 15 wake-up frame while this file exists.\r\n"), 0o644)
}

// ignitionState reports whether ODIS runs on Laertes's VCI with the wake-up configured (so the
// Ignition button can switch it), and whether the frame is switched on.
func ignitionState() (available, on bool) {
	props, _ := os.ReadFile(vciProperties())
	conf, _ := os.ReadFile(offlineConfPath)
	available = parseProperties(string(props))["vci_modulename"] == offlineVCIName && parseProperties(string(conf))["wake"] == "1"
	_, err := os.Stat(wakeFlagPath)
	return available, err == nil
}

// offlineCANLogPath is the CAN trace the offline VCI appends to (offlinevci.c canLog), read by the
// "VCI CAN" monitor. It is next to the DLL, in the DLL's own folder.
var offlineCANLogPath string // set by applyODISProduct: <odisDir>\laertes\vci-can.log

// readCANLog returns the recent lines of the VCI CAN trace (the VCI caps the file, so it is small),
// newest last, empty when there is no trace yet. Of the wake-up frames (kind W, several a second)
// only the latest few are kept, so they do not push the diagnostic traffic out. Read polls (RD, one
// per PassThruReadMsgs call, written by older VCI DLLs) are left out: they would push out the rest.
func readCANLog() []string {
	data, err := os.ReadFile(offlineCANLogPath)
	if err != nil {
		return []string{}
	}
	lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	const max, maxWake = 4000, 50
	var out []string
	wake := 0
	for i := len(lines) - 1; i >= 0 && len(out) < max; i-- {
		l := strings.TrimSpace(lines[i])
		if l == "" {
			continue
		}
		f := strings.Fields(l)
		if len(f) > 1 && f[1] == "RD" {
			continue
		}
		if len(f) > 1 && f[1] == "W" {
			if wake++; wake > maxWake {
				continue
			}
		}
		out = append(out, l)
	}
	slices.Reverse(out)
	if out == nil {
		out = []string{}
	}
	return out
}

// canTickOffset converts the trace's times to clock time: the VCI stamps each line with Windows'
// 32-bit millisecond tick (GetTickCount), so unix milliseconds = tick + this offset.
func canTickOffset() int64 {
	return time.Now().UnixMilli() - int64(uint32(windows.DurationSinceBoot().Milliseconds()))
}

// clearCANLog empties the VCI CAN trace (truncates; the VCI reopens it on its next message).
func clearCANLog() error {
	err := os.WriteFile(offlineCANLogPath, nil, 0o644)
	if os.IsNotExist(err) {
		return nil
	}
	return err
}

// wakeConfig is the periodic CAN frame the VCI sends to wake bench units (offlinevci.c wakeLoop):
// by default VW Klemmen_Status_01 with clamp 15 on. E2E is the data ID of its counter and CRC,
// "" to send Data unchanged.
type wakeConfig struct {
	On   bool
	ID   string // hex CAN ID
	Data string // hex data bytes, 1 to 8
	E2E  string // hex byte or ""
	Ms   int
}

// wakeFromSettings reads the wake-up frame from the page's settings, keeping the default for any
// value that is missing or malformed (the values end up as lines of offline.conf).
func wakeFromSettings(raw json.RawMessage) wakeConfig {
	w := wakeConfig{ID: "3C0", Data: "00000300", E2E: "C3", Ms: 100}
	w.On, _ = settingValue(raw, "wakeOn").(bool)
	hex := func(key string, minBytes, maxBytes int) (string, bool) {
		s, ok := settingValue(raw, key).(string)
		s = strings.ToUpper(strings.ReplaceAll(strings.TrimSpace(s), " ", ""))
		if !ok || len(s) < minBytes || len(s) > 2*maxBytes || strings.Trim(s, "0123456789ABCDEF") != "" {
			return "", false
		}
		return s, true
	}
	if id, ok := hex("wakeId", 1, 4); ok {
		if n, _ := strconv.ParseUint(id, 16, 32); n <= 0x1FFFFFFF {
			w.ID = id
		}
	}
	if d, ok := hex("wakeData", 2, 8); ok && len(d)%2 == 0 {
		w.Data = d
	}
	if e, set := settingValue(raw, "wakeE2E").(string); set && strings.TrimSpace(e) == "" {
		w.E2E = ""
	} else if e, ok := hex("wakeE2E", 1, 1); ok {
		w.E2E = e
	}
	if ms, ok := settingValue(raw, "wakeMs").(float64); ok && ms >= 10 && ms <= 5000 {
		w.Ms = int(ms)
	}
	return w
}

// qemuConfig connects the virtual VCI to an emulated control unit instead of the simulator: the MQB
// gateway firmware running in QEMU with its TCP CAN bridge (offlinevci.c, "QEMU gateway"). Bus is
// the emulator's CAN bus ODIS talks on, a FlexCAN number; the J533's diagnostic CAN is 4.
type qemuConfig struct {
	On   bool
	Addr string // host:port of the bridge
	Bus  int
}

// qemuBuses is the number of the gateway's FlexCAN buses (0-5; the firmware leaves CAN1 unused).
const qemuBuses = 6

// qemuFromSettings reads the QEMU gateway connection from the page's settings, keeping the default
// for any value that is missing or malformed (the values end up as lines of offline.conf).
func qemuFromSettings(raw json.RawMessage) qemuConfig {
	q := qemuConfig{Addr: "127.0.0.1:23040", Bus: 4}
	q.On, _ = settingValue(raw, "qemuOn").(bool)
	if a, ok := settingValue(raw, "qemuAddr").(string); ok {
		a = strings.TrimSpace(a)
		host, port, found := strings.Cut(a, ":")
		if p, err := strconv.Atoi(port); found && err == nil && p > 0 && p < 65536 && host != "" &&
			strings.Trim(strings.ToLower(host), "0123456789abcdefghijklmnopqrstuvwxyz.-") == "" {
			q.Addr = a
		}
	}
	if b, ok := settingValue(raw, "qemuBus").(float64); ok && b >= 0 && b < qemuBuses && b == float64(int(b)) {
		q.Bus = int(b)
	}
	return q
}

// offlineConfig is what offline.conf tells the VCI DLL: the simulated VIN, the wake-up frame and,
// for pass-through, the real J2534 driver to forward to (Passthru, named PassthruName in ODIS), or
// the QEMU gateway to connect to instead of the simulator.
type offlineConfig struct {
	VIN          string
	Wake         wakeConfig
	Passthru     string
	PassthruName string
	QEMU         qemuConfig
}

// writeOfflineConfig writes the settings the VCI DLL reads. An empty VIN omits the line, so the VCI
// keeps its built-in default; without Passthru it simulates the units.
func writeOfflineConfig(c offlineConfig) error {
	var b strings.Builder
	if vin := strings.TrimSpace(c.VIN); vin != "" {
		fmt.Fprintf(&b, "vin=%s\n", vin)
	}
	if c.Passthru != "" {
		fmt.Fprintf(&b, "passthru=%s\npassthru_name=%s\n", c.Passthru, c.PassthruName)
	} else if q := c.QEMU; q.On {
		fmt.Fprintf(&b, "qemu=%s\nqemu_bus=%d\n", q.Addr, q.Bus)
	}
	if w := c.Wake; w.On {
		fmt.Fprintf(&b, "wake=1\nwake_id=%s\nwake_data=%s\nwake_e2e=%s\nwake_ms=%d\n", w.ID, w.Data, w.E2E, w.Ms)
	}
	if err := os.MkdirAll(filepath.Dir(offlineConfPath), 0o755); err != nil {
		return err
	}
	return os.WriteFile(offlineConfPath, []byte(b.String()), 0o644)
}

// installOfflineVCI copies the DLL into place and registers it as a J2534 device. Registering
// writes HKLM, so Windows asks for administrator rights the first time.
func installOfflineVCI() error {
	if old, err := os.ReadFile(offlineVCIPath); err != nil || !bytes.Equal(old, offlineVCIDLL) {
		if err := os.MkdirAll(filepath.Dir(offlineVCIPath), 0o755); err != nil {
			return err
		}
		// A DLL that ODIS has loaded cannot be overwritten; the one in place then stays.
		if err := os.WriteFile(offlineVCIPath, offlineVCIDLL, 0o644); err != nil {
			if _, inPlace := os.Stat(offlineVCIPath); inPlace != nil {
				return err
			}
			log.Printf("offline VCI not updated, an older one stays in use until ODIS is closed: %v", err)
		}
	}
	if offlineVCIRegistered() {
		return nil
	}
	reg := filepath.Join(filepath.Dir(offlineVCIPath), "register.reg")
	if err := os.WriteFile(reg, []byte(offlineVCIReg()), 0o644); err != nil {
		return err
	}
	// Start-Process -Verb RunAs shows the UAC prompt and waits for reg.exe to finish.
	cmd := exec.Command("powershell", "-NoProfile", "-Command",
		fmt.Sprintf(`Start-Process reg.exe -ArgumentList 'import','"%s"' -Verb RunAs -Wait -WindowStyle Hidden`, reg))
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("register the offline VCI (administrator rights refused?): %v %s", err, strings.TrimSpace(string(out)))
	}
	if !offlineVCIRegistered() {
		return errors.New("the offline VCI was not registered (administrator rights refused?)")
	}
	return nil
}

func offlineVCIRegistered() bool {
	k, err := registry.OpenKey(registry.LOCAL_MACHINE, offlineVCIKey, registry.QUERY_VALUE)
	if err != nil {
		return false
	}
	defer k.Close()
	lib, _, err := k.GetStringValue("FunctionLibrary")
	return err == nil && strings.EqualFold(lib, offlineVCIPath)
}

// offlineVCIReg is the registry file that registers the offline VCI (UTF-16 is not needed: reg
// import reads "REGEDIT4" files as ANSI, and the paths are ASCII).
func offlineVCIReg() string {
	esc := strings.ReplaceAll(offlineVCIPath, `\`, `\\`)
	return "REGEDIT4\r\n\r\n[HKEY_LOCAL_MACHINE\\" + offlineVCIKey + "]\r\n" +
		`"Vendor"="Laertes"` + "\r\n" +
		`"Name"="` + offlineVCIName + `"` + "\r\n" +
		`"FunctionLibrary"="` + esc + `"` + "\r\n" +
		`"CAN"=dword:00000001` + "\r\n" + `"ISO15765"=dword:00000001` + "\r\n" +
		`"ISO9141"=dword:00000001` + "\r\n" + `"ISO14230"=dword:00000001` + "\r\n"
}

// ODIS keeps the VCI to use in vci.properties of the Windows user. An offline session points it
// at the offline VCI and keeps the user's choice in a backup, put back once that ODIS has ended.
func vciProperties() string {
	return filepath.Join(odisDir, "config", os.Getenv("USERNAME"), "vci.properties")
}

func useOfflineVCI() error {
	path := vciProperties()
	backup := path + ".laertes-online"
	if _, err := os.Stat(backup); os.IsNotExist(err) {
		if cur, err := os.ReadFile(path); err == nil {
			if err := os.WriteFile(backup, cur, 0o644); err != nil {
				return err
			}
		}
	}
	return os.WriteFile(path, []byte("#Written by Laertes for an offline session; the previous file is vci.properties.laertes-online\r\n"+
		"type_of_vci_selection=DEVICE\r\nvci_modulename="+offlineVCIName+"\r\nvci_serialnumber=\r\n"+
		"hint_vci_instance_not_configured=false\r\nvci_moduletype=J2534\r\nvci_id=\r\n"), 0o644)
}

// selectedVCI names the VCI ODIS is set to use (vci.properties), "" when none is chosen. Laertes's
// VCI is named by what it does: forwarding to the user's VCI (with the wake-up), or simulating.
func selectedVCI() string {
	data, err := os.ReadFile(vciProperties())
	if err != nil {
		return ""
	}
	name := parseSelectedVCI(string(data))
	if parseProperties(string(data))["vci_modulename"] == offlineVCIName {
		conf, _ := os.ReadFile(offlineConfPath)
		p := parseProperties(string(conf))
		if real := p["passthru_name"]; real != "" {
			return real + " via Laertes (clamp 15 wake-up)"
		}
		if addr := p["qemu"]; addr != "" {
			return "Laertes virtual VCI → QEMU gateway " + addr + ", CAN" + p["qemu_bus"]
		}
		return "Laertes virtual VCI"
	}
	return name
}

// parseProperties reads key=value lines (Java properties, offline.conf), skipping # comments.
func parseProperties(text string) map[string]string {
	values := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(line), "="); ok && !strings.HasPrefix(k, "#") {
			values[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return values
}

func parseSelectedVCI(props string) string {
	values := parseProperties(props)
	name := values["vci_modulename"]
	if name == "" {
		return ""
	}
	if t := values["vci_moduletype"]; t != "" {
		name += " (" + t + ")"
	}
	return name
}

// userJ2534Driver finds the J2534 driver of the user's own VCI choice in ODIS (kept in the backup
// while a Laertes session points ODIS at Laertes's VCI), for the pass-through with the wake-up.
func userJ2534Driver() (name, lib string, err error) {
	data, err := os.ReadFile(vciProperties() + ".laertes-online")
	if err != nil {
		data, err = os.ReadFile(vciProperties())
	}
	if err != nil {
		return "", "", errors.New("no VCI is chosen in ODIS yet: choose your VCI in ODIS first")
	}
	p := parseProperties(string(data))
	name = p["vci_modulename"]
	switch {
	case name == "":
		return "", "", errors.New("no VCI is chosen in ODIS yet: choose your VCI in ODIS first")
	case name == offlineVCIName:
		return "", "", errors.New("ODIS is set to Laertes's virtual VCI: choose your own VCI in ODIS first, then start the bridge with the wake-up")
	case !strings.EqualFold(p["vci_moduletype"], "J2534"):
		return "", "", fmt.Errorf("the clamp 15 wake-up needs a VCI that ODIS uses as a J2534 device; %s is %s", name, p["vci_moduletype"])
	}
	lib, err = j2534Library(name, p["vci_id"])
	return name, lib, err
}

// j2534Library returns the driver DLL of the registered J2534 device: the one whose registry key
// is ODIS's vci_id, else the first with the given name (two vendors' drivers may register the same
// device name).
func j2534Library(name, id string) (string, error) {
	const devices = `SOFTWARE\WOW6432Node\PassThruSupport.04.04`
	k, err := registry.OpenKey(registry.LOCAL_MACHINE, devices, registry.ENUMERATE_SUB_KEYS)
	if err != nil {
		return "", fmt.Errorf("no J2534 devices are registered: %w", err)
	}
	defer k.Close()
	keys, _ := k.ReadSubKeyNames(-1)
	if i := slices.IndexFunc(keys, func(key string) bool { return id != "" && strings.EqualFold(key, id) }); i > 0 {
		keys[0], keys[i] = keys[i], keys[0]
	}
	for _, key := range keys {
		d, err := registry.OpenKey(registry.LOCAL_MACHINE, devices+`\`+key, registry.QUERY_VALUE)
		if err != nil {
			continue
		}
		dev, _, _ := d.GetStringValue("Name")
		lib, _, _ := d.GetStringValue("FunctionLibrary")
		d.Close()
		if (strings.EqualFold(dev, name) || strings.EqualFold(key, name)) && lib != "" {
			return lib, nil
		}
	}
	return "", fmt.Errorf("the J2534 device %s is not registered in Windows", name)
}

// restoreVCI puts the user's VCI choice back after an offline session (nothing to do otherwise).
func restoreVCI() {
	path := vciProperties()
	if err := os.Rename(path+".laertes-online", path); err == nil {
		log.Printf("restored the VCI choice in %s", path)
	}
}
