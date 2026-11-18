package main

import (
	_ "embed"
	"encoding/json"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
)

// version of Laertes, shown in the web page's top bar.
const version = "0.9.16"

//go:embed index.html
var indexHTML []byte

func main() {
	setupConsoleColor()
	exe, _ := os.Executable()
	dbPath := flag.String("db", filepath.Join(filepath.Dir(exe), "laertes.db"), "SQLite database file")
	noBrowser := flag.Bool("nobrowser", false, "do not open the browser")
	bridgeAddr := flag.String("bridge", "127.0.0.1:18765", "ODIS Jython bridge (mcpserver.py) address")
	grpStub := flag.Bool("grpstub", true, "serve the stand-in for VW's online services (sign-in and GSB web service) on 127.0.0.1:23032, so online guided functions such as component protection run offline; -grpstub=false uses VW's real backend (account admin/admin)")
	flag.Parse()
	adoptOldDatabase(*dbPath)

	store, err := openStore(*dbPath)
	if err != nil {
		log.Fatalf("database: %v", err)
	}

	// Point every ODIS path at the installation chosen in the top bar (OE or OS) before anything
	// uses one; odisStarter.start re-applies it so a later switch takes effect on the next start.
	if raw, err := store.Settings(); err == nil {
		if p, ok := settingValue(raw, "odisProduct").(string); ok {
			applyODISProduct(p)
		}
	}
	log.Printf("ODIS target: %s (%s)", odisProduct, odisInstallDir)

	ln, err := net.Listen("tcp", "127.0.0.1:23031")
	if err != nil {
		ln, err = net.Listen("tcp", "127.0.0.1:0")
	}
	if err != nil {
		log.Fatal(err)
	}
	host := ln.Addr().String()
	url := "http://" + host + "/"
	log.Printf("database %s", *dbPath)
	log.Printf("Laertes %s", version)
	log.Printf("open %s", url)
	if !*noBrowser {
		exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	}
	b := &bridge{addr: *bridgeAddr}
	grp := &grpReturns{}
	grp.listen()
	if *grpStub {
		serveGRPStub()
	}
	if err := installBridge(filepath.Join(odisDir, "macros")); err != nil {
		log.Printf("bridge macro not installed: %v", err)
	}
	odis := &odisStarter{bridgeAddr: *bridgeAddr, project: func() string {
		raw, _ := store.Settings()
		p, _ := settingValue(raw, "odisProject").(string)
		return p
	}, vin: func() string {
		raw, _ := store.Settings()
		v, _ := settingValue(raw, "odisVin").(string)
		return v
	}, wake: func() wakeConfig {
		raw, _ := store.Settings()
		return wakeFromSettings(raw)
	}, qemu: func() qemuConfig {
		raw, _ := store.Settings()
		return qemuFromSettings(raw)
	}, product: func() string {
		raw, _ := store.Settings()
		p, _ := settingValue(raw, "odisProduct").(string)
		return p
	}}
	if raw, err := store.Settings(); err == nil {
		if on, set := settingValue(raw, "autoStartBridge").(bool); on || !set {
			// Offline, or a bench unit that cannot be identified by a VIN: the chosen project is
			// loaded directly.
			project := ""
			offline, _ := settingValue(raw, "odisOffline").(bool)
			direct, _ := settingValue(raw, "odisDirectProject").(bool)
			if offline || direct {
				project = odis.project()
			}
			odis.start(project, offline)
		}
	}
	log.Fatal(http.Serve(ln, sameOrigin(host, singleClient(logRequests(routes(store, b, grp, odis, filepath.Dir(*dbPath)))))))
}

// adoptOldDatabase renames the database of the tool's former name (odishelper.db, with its
// SQLite -wal and -shm files) to path, unless a database already exists at path.
func adoptOldDatabase(path string) {
	old := filepath.Join(filepath.Dir(path), "odishelper.db")
	if _, err := os.Stat(path); err == nil {
		return
	}
	if _, err := os.Stat(old); err != nil {
		return
	}
	// All files move or none: a database separated from its -wal file loses recent changes.
	var moved []string
	for _, suffix := range []string{"", "-wal", "-shm"} {
		err := os.Rename(old+suffix, path+suffix)
		if err == nil {
			moved = append(moved, suffix)
		} else if !os.IsNotExist(err) {
			for _, m := range moved {
				os.Rename(path+m, old+m)
			}
			log.Fatalf("rename %s to %s (is the old odishelper.exe still running?): %v", old+suffix, path+suffix, err)
		}
	}
	log.Printf("renamed %s to %s", old, path)
}

// sameOrigin rejects requests from other web pages (CSRF) and DNS-rebinding hosts,
// since this API can write into the ODIS process.
func sameOrigin(host string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if r.Host != host || (r.Method != http.MethodGet && origin != "http://"+host) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func routes(store *Store, b *bridge, grp *grpReturns, odis *odisStarter, dataDir string) http.Handler {
	mux := http.NewServeMux()
	// Starting the bridge macro in ODIS without the user (see odisStarter).
	mux.HandleFunc("GET /api/odis", func(w http.ResponseWriter, r *http.Request) {
		reply(w, odis.status(), nil)
	})
	mux.HandleFunc("GET /api/odis/projects", func(w http.ResponseWriter, r *http.Request) {
		reply(w, odisProjects(), nil)
	})
	mux.HandleFunc("POST /api/odis/start", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Project string `json:"project"`
			Offline bool   `json:"offline"`
		}
		json.NewDecoder(io.LimitReader(r.Body, 1<<12)).Decode(&req)
		if req.Offline {
			log.Printf("start bridge requested: virtual VCI, vehicle project %q", strings.TrimSpace(req.Project))
		} else {
			log.Printf("start bridge requested: own VCI (%s)", selectedVCI())
		}
		odis.start(strings.TrimSpace(req.Project), req.Offline)
		reply(w, odis.status(), nil)
	})
	// Opens the engine.log of the selected ODIS (OE or OS) in its default viewer, so the user can
	// watch what ODIS is doing. Laertes runs on the same PC, so it can open the file directly.
	mux.HandleFunc("POST /api/odis/logs", func(w http.ResponseWriter, r *http.Request) {
		path := filepath.Join(odisDir, "log", "engine.log")
		if _, err := os.Stat(path); err != nil {
			http.Error(w, "the ODIS log "+path+" does not exist yet (start the bridge first)", http.StatusNotFound)
			return
		}
		if err := exec.Command("rundll32", "url.dll,FileProtocolHandler", path).Start(); err != nil {
			reply(w, nil, err)
			return
		}
		log.Printf("opened ODIS log %s", path)
		reply(w, map[string]string{"path": path, "product": odisProduct}, nil)
	})
	// Car backups (backups.go): the list, a file to download, and opening one on this PC.
	mux.HandleFunc("GET /api/backups", func(w http.ResponseWriter, r *http.Request) {
		list, err := listBackups(backupDir())
		reply(w, map[string]any{"dir": backupDir(), "backups": list}, err)
	})
	mux.HandleFunc("GET /api/backups/{name}", func(w http.ResponseWriter, r *http.Request) {
		path := backupPath(backupDir(), r.PathValue("name"))
		if path == "" {
			http.Error(w, "not a backup file", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Disposition", `attachment; filename="`+r.PathValue("name")+`"`)
		http.ServeFile(w, r, path)
	})
	mux.HandleFunc("POST /api/backups/{name}/open", func(w http.ResponseWriter, r *http.Request) {
		path := backupPath(backupDir(), r.PathValue("name"))
		if path == "" {
			http.Error(w, "not a backup file", http.StatusBadRequest)
			return
		}
		if _, err := os.Stat(path); err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		if err := exec.Command("rundll32", "url.dll,FileProtocolHandler", path).Start(); err != nil {
			reply(w, nil, err)
			return
		}
		log.Printf("opened backup %s", path)
		reply(w, map[string]string{"path": path}, nil)
	})
	// ODIS's .html protocols of flashes and data transfers (backups.go): the list, one to view in
	// the browser, and opening one on this PC.
	mux.HandleFunc("GET /api/protocols", func(w http.ResponseWriter, r *http.Request) {
		list, err := listProtocols(backupDir())
		reply(w, map[string]any{"dir": backupDir(), "protocols": list, "now": time.Now().Format(time.RFC3339Nano)}, err)
	})
	mux.HandleFunc("GET /api/protocols/{name}", func(w http.ResponseWriter, r *http.Request) {
		path := protocolPath(backupDir(), r.PathValue("name"))
		if path == "" {
			http.Error(w, "not an ODIS protocol", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		http.ServeFile(w, r, path)
	})
	mux.HandleFunc("POST /api/protocols/{name}/open", func(w http.ResponseWriter, r *http.Request) {
		path := protocolPath(backupDir(), r.PathValue("name"))
		if path == "" {
			http.Error(w, "not an ODIS protocol", http.StatusBadRequest)
			return
		}
		if _, err := os.Stat(path); err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		if err := exec.Command("rundll32", "url.dll,FileProtocolHandler", path).Start(); err != nil {
			reply(w, nil, err)
			return
		}
		log.Printf("opened ODIS protocol %s", path)
		reply(w, map[string]string{"path": path}, nil)
	})
	// The offline VCI's CAN trace (offlinevci.c canLog): the "VCI CAN" tab polls it to show the
	// ISO15765 traffic between ODIS and the simulated units.
	mux.HandleFunc("GET /api/vci/can", func(w http.ResponseWriter, r *http.Request) {
		reply(w, map[string]any{"lines": readCANLog(), "tickOffset": canTickOffset()}, nil)
	})
	// The simulated car of the virtual VCI (simcar.go): a VCDS Auto-Scan turned into car.txt.
	mux.HandleFunc("GET /api/vci/car", func(w http.ResponseWriter, r *http.Request) {
		car, err := store.SimCar()
		_, statErr := os.Stat(simCarPath())
		reply(w, map[string]any{"active": statErr == nil, "car": car}, err)
	})
	mux.HandleFunc("POST /api/vci/car", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Scan   string            `json:"scan"`
			Snames map[string]string `json:"snames"` // unit address ("09") -> ODIS short name in the project
			Nodes  map[string]int    `json:"nodes"`  // logical link -> gateway node address (bridge gateway_nodes)
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&req); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		car, err := setSimCar(req.Scan, req.Snames, req.Nodes)
		if err != nil {
			reply(w, nil, err)
			return
		}
		log.Printf("virtual VCI simulates %s: %d units", car.VIN, len(car.Units))
		b, _ := json.Marshal(car)
		reply(w, car, store.SetSimCar(b))
	})
	mux.HandleFunc("DELETE /api/vci/car", func(w http.ResponseWriter, r *http.Request) {
		if err := clearSimCar(); err != nil {
			reply(w, nil, err)
			return
		}
		reply(w, nil, store.SetSimCar(json.RawMessage("{}")))
	})
	// The Ignition button: switches the VCI's periodic clamp 15 wake-up frame on or off, live.
	mux.HandleFunc("POST /api/vci/ignition", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			On bool `json:"on"`
		}
		json.NewDecoder(io.LimitReader(r.Body, 1<<10)).Decode(&req)
		if available, _ := ignitionState(); !available {
			http.Error(w, "the clamp 15 wake-up is not active: turn it on in Settings and start the bridge again", http.StatusConflict)
			return
		}
		err := setIgnition(req.On)
		if err == nil {
			log.Printf("ignition (wake-up frame) %s", map[bool]string{true: "on", false: "off"}[req.On])
		}
		reply(w, odis.status(), err)
	})
	mux.HandleFunc("POST /api/vci/can/clear", func(w http.ResponseWriter, r *http.Request) {
		reply(w, nil, clearCANLog())
	})
	// Browse the ODIS PC for a file to flash: lists one directory (folders first). Laertes runs
	// on the same PC, so these are the paths the bridge can read.
	mux.HandleFunc("GET /api/files", func(w http.ResponseWriter, r *http.Request) {
		dir := r.URL.Query().Get("dir")
		if dir == "" {
			if h, err := os.UserHomeDir(); err == nil {
				dir = h
			} else {
				dir = dataDir
			}
		}
		dir = filepath.Clean(dir)
		ents, err := os.ReadDir(dir)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		type entry struct {
			Name string `json:"name"`
			Dir  bool   `json:"dir"`
			Size int64  `json:"size"`
		}
		list := []entry{}
		for _, e := range ents {
			info, _ := e.Info()
			var size int64
			if info != nil {
				size = info.Size()
			}
			list = append(list, entry{e.Name(), e.IsDir(), size})
		}
		sort.Slice(list, func(i, j int) bool {
			if list[i].Dir != list[j].Dir {
				return list[i].Dir
			}
			return strings.ToLower(list[i].Name) < strings.ToLower(list[j].Name)
		})
		reply(w, map[string]any{"dir": dir, "parent": filepath.Dir(dir), "entries": list}, nil)
	})
	// Upload a file from the browser into <dataDir>/uploads and return its path on the PC, so it
	// can be flashed. Streamed to disk; the file never has to exist on the PC beforehand.
	mux.HandleFunc("POST /api/upload", func(w http.ResponseWriter, r *http.Request) {
		name := filepath.Base(r.URL.Query().Get("name"))
		if name == "" || name == "." || name == string(filepath.Separator) || strings.ContainsAny(name, `/\`) {
			http.Error(w, "bad file name", http.StatusBadRequest)
			return
		}
		up := filepath.Join(dataDir, "uploads")
		if err := os.MkdirAll(up, 0o755); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		dst := filepath.Join(up, name)
		f, err := os.Create(dst)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		defer f.Close()
		if _, err := io.Copy(f, io.LimitReader(r.Body, 2<<30)); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		reply(w, map[string]any{"path": dst, "name": name}, nil)
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(indexHTML)
	})
	// The Vehicles tab's tables, read from the ODIS installation chosen in the top bar (?product=OE|OS).
	mux.HandleFunc("GET /api/vehicles", func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Query().Get("product")
		if p != "OS" {
			p = "OE"
		}
		db, err := loadVehicles(filepath.Join(envOr("ProgramData", `C:\ProgramData`), p))
		reply(w, db, err)
	})
	mux.HandleFunc("GET /api/version", func(w http.ResponseWriter, r *http.Request) {
		reply(w, version, nil)
	})
	mux.HandleFunc("GET /api/labels", func(w http.ResponseWriter, r *http.Request) {
		ls, err := store.Labels(r.URL.Query().Get("variant"), r.URL.Query().Get("subsystem"))
		reply(w, ls, err)
	})
	mux.HandleFunc("PUT /api/labels", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Variant   string  `json:"variant"`
			Subsystem string  `json:"subsystem"`
			Labels    []Label `json:"labels"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		for _, l := range req.Labels {
			if !l.valid() {
				http.Error(w, "bad label", http.StatusBadRequest)
				return
			}
		}
		reply(w, nil, store.SetLabels(req.Variant, req.Subsystem, req.Labels))
	})
	mux.HandleFunc("POST /api/labels/import", func(w http.ResponseWriter, r *http.Request) {
		src, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		ls, err := parseLBL(latin1(src))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		q := r.URL.Query()
		reply(w, len(ls), store.SetLabels(q.Get("variant"), q.Get("subsystem"), ls))
	})
	mux.HandleFunc("GET /api/history", func(w http.ResponseWriter, r *http.Request) {
		// ?retrofit=<id> returns every change of that retrofit; otherwise the latest 500 of all.
		if q := r.URL.Query().Get("retrofit"); q != "" {
			id, err := strconv.ParseInt(q, 10, 64)
			if err != nil || id < 0 {
				http.Error(w, "bad retrofit id", http.StatusBadRequest)
				return
			}
			cs, err := store.Changes(100000, id)
			reply(w, cs, err)
			return
		}
		cs, err := store.Changes(500, -1)
		reply(w, cs, err)
	})
	mux.HandleFunc("POST /api/history", func(w http.ResponseWriter, r *http.Request) {
		var c Change
		if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if c.Time == "" {
			c.Time = time.Now().Format(time.RFC3339)
		}
		reply(w, &c, store.AddChange(&c))
	})
	mux.HandleFunc("DELETE /api/history/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
		if err != nil || id <= 0 {
			http.Error(w, "bad id", http.StatusBadRequest)
			return
		}
		reply(w, nil, store.DeleteChanges(id))
	})
	mux.HandleFunc("DELETE /api/history", func(w http.ResponseWriter, r *http.Request) {
		reply(w, nil, store.DeleteChanges(0))
	})
	mux.HandleFunc("GET /api/retrofits", func(w http.ResponseWriter, r *http.Request) {
		rs, err := store.Retrofits()
		reply(w, rs, err)
	})
	mux.HandleFunc("GET /api/retrofits/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
		if err != nil {
			http.Error(w, "bad id", http.StatusBadRequest)
			return
		}
		rf, err := store.Retrofit(id)
		if err == nil && rf == nil {
			http.Error(w, "no such retrofit", http.StatusNotFound)
			return
		}
		reply(w, rf, err)
	})
	mux.HandleFunc("POST /api/retrofits", func(w http.ResponseWriter, r *http.Request) {
		var rf Retrofit
		// The baseline is a full-scan log of the whole car, so allow a large body.
		if err := json.NewDecoder(io.LimitReader(r.Body, 32<<20)).Decode(&rf); err != nil || rf.Name == "" ||
			(len(rf.BaselineDTCs) > 0 && !json.Valid(rf.BaselineDTCs)) {
			http.Error(w, "a retrofit needs a name", http.StatusBadRequest)
			return
		}
		rf.Started, rf.Ended = time.Now().Format(time.RFC3339), ""
		if err := store.StartRetrofit(&rf); err == errRetrofitActive {
			http.Error(w, err.Error(), http.StatusConflict)
		} else {
			reply(w, &rf, err)
		}
	})
	mux.HandleFunc("PATCH /api/retrofits/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
		var req struct {
			Name string `json:"name"`
			End  bool   `json:"end"`
		}
		if err != nil || json.NewDecoder(r.Body).Decode(&req) != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		ended := ""
		if req.End {
			ended = time.Now().Format(time.RFC3339)
		}
		reply(w, nil, store.UpdateRetrofit(id, strings.TrimSpace(req.Name), ended))
	})
	mux.HandleFunc("GET /api/settings", func(w http.ResponseWriter, r *http.Request) {
		v, err := store.Settings()
		reply(w, v, err)
	})
	mux.HandleFunc("PUT /api/settings", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<16))
		if err != nil || !json.Valid(body) {
			http.Error(w, "settings must be a JSON object", http.StatusBadRequest)
			return
		}
		reply(w, nil, store.SetSettings(body))
	})
	// The user's edits of the coding <-> trouble code rules (Rules tab).
	mux.HandleFunc("GET /api/coding-rules", func(w http.ResponseWriter, r *http.Request) {
		v, err := store.CodingRules()
		reply(w, v, err)
	})
	mux.HandleFunc("PUT /api/coding-rules", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil || !json.Valid(body) {
			http.Error(w, "coding rules must be a JSON object", http.StatusBadRequest)
			return
		}
		reply(w, nil, store.SetCodingRules(body))
	})
	// The GeKo sign-in for ODIS's online functions. Sent back to the page in full: only this
	// PC's own Laertes page can read it (sameOrigin, singleClient).
	mux.HandleFunc("GET /api/geko", func(w http.ResponseWriter, r *http.Request) {
		g, err := store.Geko()
		reply(w, g, err)
	})
	mux.HandleFunc("PUT /api/geko", func(w http.ResponseWriter, r *http.Request) {
		var g Geko
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&g); err != nil {
			http.Error(w, "GeKo sign-in must be a JSON object with username and password", http.StatusBadRequest)
			return
		}
		reply(w, nil, store.SetGeko(g))
	})
	mux.HandleFunc("GET /api/coding-pref", func(w http.ResponseWriter, r *http.Request) {
		p, err := store.CodingPref(r.URL.Query().Get("variant"))
		if err != nil {
			reply(w, nil, err)
			return
		}
		if p == nil {
			reply(w, map[string]any{}, nil)
			return
		}
		reply(w, p, nil)
	})
	// Coverage tab: the last check (kept so its list shows without the bridge), every borrowed
	// coding definition and every set of .lbl labels.
	mux.HandleFunc("GET /api/coverage", func(w http.ResponseWriter, r *http.Request) {
		v, err := store.Coverage()
		reply(w, v, err)
	})
	mux.HandleFunc("PUT /api/coverage", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
		if err != nil || !json.Valid(body) {
			http.Error(w, "coverage must be a JSON object", http.StatusBadRequest)
			return
		}
		reply(w, nil, store.SetCoverage(body))
	})
	mux.HandleFunc("GET /api/coding-prefs", func(w http.ResponseWriter, r *http.Request) {
		ps, err := store.CodingPrefs()
		reply(w, ps, err)
	})
	mux.HandleFunc("GET /api/labels/summary", func(w http.ResponseWriter, r *http.Request) {
		ls, err := store.LabelSets()
		reply(w, ls, err)
	})
	mux.HandleFunc("PUT /api/coding-pref", func(w http.ResponseWriter, r *http.Request) {
		var p CodingPref
		if err := json.NewDecoder(r.Body).Decode(&p); err != nil || p.Variant == "" || p.BorrowVariant == "" {
			http.Error(w, "variant and borrowVariant are required", http.StatusBadRequest)
			return
		}
		reply(w, nil, store.SetCodingPref(p))
	})
	mux.HandleFunc("DELETE /api/coding-pref", func(w http.ResponseWriter, r *http.Request) {
		variant := r.URL.Query().Get("variant")
		if variant == "" {
			http.Error(w, "variant is required", http.StatusBadRequest)
			return
		}
		reply(w, nil, store.DeleteCodingPref(variant))
	})
	// The address the browser came back to at the end of a VW online sign-in (see grpReturns).
	mux.HandleFunc("GET /api/grp-return", func(w http.ResponseWriter, r *http.Request) {
		reply(w, map[string]string{"url": grp.take(r.URL.Query().Get("state"))}, nil)
	})
	mux.HandleFunc("POST /api/bridge", func(w http.ResponseWriter, r *http.Request) {
		var cmd struct {
			Action string `json:"action"`
		}
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<16))
		if err != nil || json.Unmarshal(body, &cmd) != nil || cmd.Action == "" {
			http.Error(w, "bridge command must be a JSON object with an action", http.StatusBadRequest)
			return
		}
		start := time.Now()
		res, err := b.call(body)
		// The page polls ping, vehicle and vci every few seconds; logging them would bury the rest.
		if !slices.Contains([]string{"ping", "vehicle", "vci"}, cmd.Action) {
			if err != nil {
				log.Printf("bridge %s failed after %v: %v", cmd.Action, time.Since(start).Round(time.Millisecond), err)
			} else {
				log.Printf("bridge %s done in %v", cmd.Action, time.Since(start).Round(time.Millisecond))
			}
		}
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(res)
	})
	return mux
}

// logRequests logs the page's API calls with their outcome in the console, except the status
// polls (and /api/bridge, which logs its action itself). A failed call also logs its error text.
func logRequests(next http.Handler) http.Handler {
	quiet := []string{"GET /api/odis", "GET /api/vci/can", "GET /api/version", "POST /api/bridge"}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		req := r.Method + " " + r.URL.Path
		if !strings.HasPrefix(r.URL.Path, "/api/") || slices.Contains(quiet, req) {
			next.ServeHTTP(w, r)
			return
		}
		start := time.Now()
		lw := &loggedWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(lw, r)
		took := time.Since(start).Round(time.Millisecond)
		if lw.status >= 400 {
			log.Printf("%s -> %d in %v: %s", req, lw.status, took, strings.TrimSpace(lw.body.String()))
		} else {
			log.Printf("%s -> %d in %v", req, lw.status, took)
		}
	})
}

// loggedWriter records a response's status, and the start of an error response's text.
type loggedWriter struct {
	http.ResponseWriter
	status int
	body   strings.Builder
}

func (w *loggedWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}

func (w *loggedWriter) Write(b []byte) (int, error) {
	if w.status >= 400 && w.body.Len() < 300 {
		w.body.Write(b[:min(len(b), 300-w.body.Len())])
	}
	return w.ResponseWriter.Write(b)
}

func reply(w http.ResponseWriter, v any, err error) {
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
