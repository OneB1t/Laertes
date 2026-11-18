package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestListBackups(t *testing.T) {
	dir := t.TempDir()
	write := func(name string, age time.Duration) {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		at := time.Now().Add(-age)
		os.Chtimes(p, at, at)
	}
	write("Cod_Anp_WVWZZZAUZEW042267_20261017T174235.xml", 2*time.Hour)
	write("Cod_Anp_WVWZZZAUZEW042267_20261017T174235.html", 2*time.Hour)
	write("Cod_Anp_WVWZZZAUZEW042267_20261028T025642.xml", time.Hour)
	write("WVWZZZAUZEW042267_20261017T174105.xml", 0) // a diagnosis protocol, not a backup
	list, err := listBackups(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 || list[0].Name != "Cod_Anp_WVWZZZAUZEW042267_20261028T025642.xml" || list[0].HTML != "" ||
		list[1].HTML != "Cod_Anp_WVWZZZAUZEW042267_20261017T174235.html" {
		t.Fatalf("list = %+v", list)
	}
	if list, err := listBackups(filepath.Join(dir, "missing")); err != nil || len(list) != 0 {
		t.Fatalf("missing dir: %v %v", list, err)
	}
}

func TestBackupPath(t *testing.T) {
	for name, ok := range map[string]bool{
		"Cod_Anp_WVWZZZAUZEW042267_20261028T025642.xml":  true,
		"Cod_Anp_WVWZZZAUZEW042267_20261028T025642.html": true,
		`..\Cod_Anp_x.xml`:  false,
		"Cod_Anp_x.xml.exe": false,
		"laertes.db":        false,
	} {
		if got := backupPath(`C:\d`, name) != ""; got != ok {
			t.Errorf("backupPath(%q) ok = %v, want %v", name, got, ok)
		}
	}
}

func TestProtocols(t *testing.T) {
	dir := t.TempDir()
	for _, n := range []string{"Dataset_WVWZZZ1KZAW000001_20261028T221904.html", "Dataset_WVWZZZ1KZAW000001_20261028T221904.xml", "ODIS_E_PROTOCOL.xsl"} {
		if err := os.WriteFile(filepath.Join(dir, n), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	list, err := listProtocols(dir)
	if err != nil || len(list) != 1 || list[0].Name != "Dataset_WVWZZZ1KZAW000001_20261028T221904.html" {
		t.Fatalf("list = %+v %v", list, err)
	}
	for name, ok := range map[string]bool{
		"Dataset_WVWZZZ1KZAW000001_20261028T221904.html": true,
		`..\x.html`:  false,
		"../x.html":  false,
		"..html":     false,
		"x.html.exe": false,
		"laertes.db": false,
		".html":      false,
	} {
		if got := protocolPath(`C:\d`, name) != ""; got != ok {
			t.Errorf("protocolPath(%q) ok = %v, want %v", name, got, ok)
		}
	}
}
