// Command genfixtures regenerates the synthetic captures under testdata/.
// Each fixture writes <name>.jsonl (the capture) and <name>.expected.json
// (the independently derived golden answer from internal/oracle).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"tcpreasm/internal/fixture"
)

func main() {
	dir := flag.String("out", "testdata", "output directory")
	only := flag.String("only", "", "comma-separated fixture names to regenerate")
	flag.Parse()
	if err := os.MkdirAll(*dir, 0o755); err != nil {
		fail(err)
	}
	want := map[string]bool{}
	for _, n := range strings.Split(*only, ",") {
		if n = strings.TrimSpace(n); n != "" {
			want[n] = true
		}
	}
	for _, bl := range fixture.All() {
		if len(want) > 0 && !want[bl.Spec.Name] {
			continue
		}
		base := filepath.Join(*dir, bl.Spec.Name)
		if err := writeCapture(base+".jsonl", bl); err != nil {
			fail(err)
		}
		if err := writeExpected(base+".expected.json", bl); err != nil {
			fail(err)
		}
		fmt.Println("wrote", bl.Describe())
	}
}

func writeCapture(path string, bl fixture.Built) error {
	var sb strings.Builder
	sb.WriteString("# synthetic TCP capture for fixture " + bl.Spec.Name + "\n")
	sb.WriteString("# " + bl.Spec.Description + "\n")
	for _, op := range bl.Packets {
		mp := fixture.ToModel(bl.Flow, op)
		line, err := json.Marshal(mp)
		if err != nil {
			return err
		}
		sb.Write(line)
		sb.WriteByte('\n')
	}
	return os.WriteFile(path, []byte(sb.String()), 0o644)
}

func writeExpected(path string, bl fixture.Built) error {
	type envelope struct {
		Flow struct {
			Client string `json:"client"`
			Server string `json:"server"`
			ISNC2S uint32 `json:"isn_c2s"`
			ISNS2C uint32 `json:"isn_s2c"`
		} `json:"flow"`
		fixture.FixtureSpec
	}
	env := envelope{FixtureSpec: bl.Spec}
	env.Flow.Client = bl.Flow.Client.String()
	env.Flow.Server = bl.Flow.Server.String()
	env.Flow.ISNC2S = bl.Flow.ISNC2S
	env.Flow.ISNS2C = bl.Flow.ISNS2C
	raw, err := json.MarshalIndent(env, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(raw, '\n'), 0o644)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "genfixtures:", err)
	os.Exit(1)
}
