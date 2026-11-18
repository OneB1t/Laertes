package main

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// Car backups are the result protocols of ODIS's 046.01 Coding / Adaptations "Read data"
// (bridge action car_backup): Cod_Anp_<VIN>_<time>.xml, the file ODIS can write back to the units,
// and the .html view ODIS renders next to it. They live in ODIS's DiagnosticProtocols folder,
// where ODIS writes its own protocols too.

// backupName matches the files of a car backup; the page names a file only by this base name.
var backupName = regexp.MustCompile(`^Cod_Anp_[A-Za-z0-9_-]+\.(xml|html)$`)

func backupDir() string { return filepath.Join(odisDir, "DiagnosticProtocols") }

// Backup is one backup in the list: its .xml and whether ODIS also rendered the .html.
type Backup struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
	Time string `json:"time"`
	HTML string `json:"html,omitempty"`
}

// listBackups lists the backups in dir, newest first. A missing dir is an empty list.
func listBackups(dir string) ([]Backup, error) {
	ents, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return []Backup{}, nil
	}
	if err != nil {
		return nil, err
	}
	names := map[string]bool{}
	for _, e := range ents {
		names[e.Name()] = true
	}
	list := []Backup{}
	for _, e := range ents {
		if !backupName.MatchString(e.Name()) || !strings.HasSuffix(e.Name(), ".xml") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		b := Backup{Name: e.Name(), Size: info.Size(), Time: info.ModTime().Format(time.RFC3339)}
		if html := strings.TrimSuffix(e.Name(), ".xml") + ".html"; names[html] {
			b.HTML = html
		}
		list = append(list, b)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Time > list[j].Time })
	return list, nil
}

// backupPath is the path of backup file name in dir, or "" when name is not a backup file's name.
func backupPath(dir, name string) string {
	if !backupName.MatchString(name) {
		return ""
	}
	return filepath.Join(dir, name)
}

// ODIS also writes a protocol of each flash and data transfer it runs (Dataset_<VIN>_<time>.xml,
// and the .html view of it) to the same DiagnosticProtocols folder. The page lists the .html
// reports before a job and again when it ends, so it can link the report the job produced.

// protocolName matches an .html report in DiagnosticProtocols; the page names one only by this
// base name, so a name can never leave the folder.
var protocolName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*\.html$`)

// Protocol is one .html report ODIS wrote.
type Protocol struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
	Time string `json:"time"`
}

// listProtocols lists the .html reports in dir, newest first. A missing dir is an empty list.
func listProtocols(dir string) ([]Protocol, error) {
	ents, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return []Protocol{}, nil
	}
	if err != nil {
		return nil, err
	}
	list := []Protocol{}
	for _, e := range ents {
		if e.IsDir() || !protocolName.MatchString(e.Name()) {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		list = append(list, Protocol{Name: e.Name(), Size: info.Size(), Time: info.ModTime().Format(time.RFC3339Nano)})
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Time > list[j].Time })
	return list, nil
}

// protocolPath is the path of report name in dir, or "" when name is not a report's name.
func protocolPath(dir, name string) string {
	if !protocolName.MatchString(name) || strings.Contains(name, "..") {
		return ""
	}
	return filepath.Join(dir, name)
}
