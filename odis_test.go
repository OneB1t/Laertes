package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestInstallBridge(t *testing.T) {
	oe := filepath.Join(t.TempDir(), "OE")
	macros := filepath.Join(oe, "macros")
	if err := installBridge(macros); err == nil {
		t.Fatal("installed although ODIS's folder is missing")
	}
	os.Mkdir(oe, 0o755)
	os.Mkdir(macros, 0o755)
	os.WriteFile(filepath.Join(macros, "mcpserver_handlers.py"), []byte("old"), 0o644)
	if err := installBridge(macros); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string][]byte{"mcpserver.py": bridgeMacro, "mcpserver_handlers.py": bridgeHandlers} {
		got, err := os.ReadFile(filepath.Join(macros, name))
		if err != nil || !bytes.Equal(got, want) {
			t.Errorf("%s not installed (%v)", name, err)
		}
	}
	if left, _ := filepath.Glob(filepath.Join(macros, "*.laertes-tmp")); len(left) > 0 {
		t.Errorf("temporary files left: %v", left)
	}
}

func TestSoapFault(t *testing.T) {
	fault := `<?xml version='1.0' encoding='UTF-8'?><S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body><S:Fault><faultcode>S:Server</faultcode><faultstring>ODS8014E
Project not available.</faultstring></S:Fault></S:Body></S:Envelope>`
	if got := soapFault([]byte(fault)); got != "ODS8014E Project not available." {
		t.Errorf("fault = %q", got)
	}
	ok := `<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body><ns2:exitResponse xmlns:ns2="x"/></S:Body></S:Envelope>`
	if got := soapFault([]byte(ok)); got != "" {
		t.Errorf("no fault expected, got %q", got)
	}
}

func TestWakeFromSettings(t *testing.T) {
	def := wakeConfig{ID: "3C0", Data: "00000300", E2E: "C3", Ms: 100}
	if got := wakeFromSettings(nil); got != def {
		t.Errorf("defaults = %+v", got)
	}
	got := wakeFromSettings([]byte(`{"wakeOn":true,"wakeId":"1b000010","wakeData":"00 01 02","wakeE2E":"","wakeMs":200}`))
	if want := (wakeConfig{On: true, ID: "1B000010", Data: "000102", E2E: "", Ms: 200}); got != want {
		t.Errorf("wake = %+v, want %+v", got, want)
	}
	// Malformed values (and anything that could add a line to offline.conf) keep the defaults.
	got = wakeFromSettings([]byte(`{"wakeId":"3C0\npassthru=x","wakeData":"0G","wakeE2E":"C3C3","wakeMs":1}`))
	if got != def {
		t.Errorf("malformed = %+v", got)
	}
}

func TestQEMUFromSettings(t *testing.T) {
	def := qemuConfig{Addr: "127.0.0.1:23040", Bus: 4}
	if got := qemuFromSettings(nil); got != def {
		t.Errorf("defaults = %+v", got)
	}
	got := qemuFromSettings([]byte(`{"qemuOn":true,"qemuAddr":" localhost:24000 ","qemuBus":0}`))
	if want := (qemuConfig{On: true, Addr: "localhost:24000", Bus: 0}); got != want {
		t.Errorf("qemu = %+v, want %+v", got, want)
	}
	// Malformed values (and anything that could add a line to offline.conf) keep the defaults.
	got = qemuFromSettings([]byte(`{"qemuAddr":"x:1\npassthru=y","qemuBus":9}`))
	if got != def {
		t.Errorf("malformed = %+v", got)
	}
	for _, a := range []string{"host", "host:0", "host:99999", ":23040"} {
		if got := qemuFromSettings([]byte(`{"qemuAddr":"` + a + `"}`)); got.Addr != def.Addr {
			t.Errorf("address %q accepted", a)
		}
	}
}

func TestReadCANLog(t *testing.T) {
	old := offlineCANLogPath
	offlineCANLogPath = filepath.Join(t.TempDir(), "vci-can.log")
	defer func() { offlineCANLogPath = old }()
	var b strings.Builder
	b.WriteString("1 R 8 700 03222A2F55555555\n")
	for i := 0; i < 5000; i++ {
		b.WriteString("2 RD ch=8 to=0 req=1 got=0\n")
	}
	for i := 0; i < 80; i++ {
		b.WriteString("3 W 8 3C0 74000300\n")
	}
	b.WriteString("4 T 8 76A 622A2F00\n")
	os.WriteFile(offlineCANLogPath, []byte(b.String()), 0o644)
	lines := readCANLog()
	if len(lines) != 52 || lines[0] != "1 R 8 700 03222A2F55555555" || lines[51] != "4 T 8 76A 622A2F00" {
		t.Errorf("got %d lines, first %q, last %q", len(lines), lines[0], lines[len(lines)-1])
	}
}

func TestParseSelectedVCI(t *testing.T) {
	props := "#Do not change\r\ntype_of_vci_selection=DEVICE\r\nvci_modulename=Laertes-Offline\r\nvci_moduletype=J2534\r\n"
	if got := parseSelectedVCI(props); got != "Laertes-Offline (J2534)" {
		t.Errorf("vci = %q", got)
	}
	if got := parseSelectedVCI("type_of_vci_selection=DEVICE\nvci_modulename=\n"); got != "" {
		t.Errorf("no VCI expected, got %q", got)
	}
}

func TestStartTimer(t *testing.T) {
	o := &odisStarter{since: time.Now().Add(-2 * time.Second)}
	o.set("starting", "Loading vehicle project")
	if st := o.status(); st.Elapsed < 2 || st.Took != 0 {
		t.Errorf("while starting: %+v", st)
	}
	o.set("running", "The ODIS bridge is running.")
	if st := o.status(); st.Elapsed != 0 || st.Took < 2 {
		t.Errorf("after the start: %+v", st)
	}
}
