package replay

import (
	"os"
	"path/filepath"
	"sort"
	"strings"

	"flowrouter/internal/apperr"
)

// FileLibrary serves named flow sets from a directory of *.json files. It is
// a local synthetic dependency: fixtures are committed under testdata and
// configs; nothing is fetched over the network.
type FileLibrary struct {
	dir string
}

func NewFileLibrary(dir string) *FileLibrary { return &FileLibrary{dir: dir} }

// Load reads <dir>/<name>.json (no path traversal: name must be a base name).
func (l *FileLibrary) Load(name string) (*FlowSet, error) {
	if name == "" || name != filepath.Base(name) || strings.ContainsAny(name, `/\`) {
		return nil, apperr.Invalid("FLOWSET_BAD_NAME", "flow set name must be a plain file base name")
	}
	raw, err := os.ReadFile(filepath.Join(l.dir, name+".json"))
	if err != nil {
		return nil, apperr.Invalid("FLOWSET_UNKNOWN", "unknown flow set: "+name).WithCause(err)
	}
	return ParseFlowSet(raw)
}

// Names lists available flow sets alphabetically.
func (l *FileLibrary) Names() ([]string, error) {
	entries, err := os.ReadDir(l.dir)
	if err != nil {
		return nil, apperr.Invalid("FLOWSET_DIR", "cannot read flow set directory").WithCause(err)
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".json") {
			names = append(names, strings.TrimSuffix(e.Name(), ".json"))
		}
	}
	sort.Strings(names)
	return names, nil
}
