package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"

	"ipreasm/internal/replay"
	"ipreasm/internal/store"
)

// The types below duplicate the manifest schema intentionally: the
// checker must not import any code from cmd/genfixture, which produced
// the expectations.

type mKey struct {
	Src   string `json:"src"`
	Dst   string `json:"dst"`
	Proto int    `json:"proto"`
	ID    int    `json:"id"`
}

type mExpect struct {
	Key        mKey   `json:"key"`
	Expect     string `json:"expect"`
	Reason     string `json:"reason,omitempty"`
	SHA256     string `json:"sha256,omitempty"`
	Length     int    `json:"length,omitempty"`
	Frags      int    `json:"frags,omitempty"`
	Duplicates int    `json:"duplicate_fragments,omitempty"`
	GoldenFile string `json:"golden_file,omitempty"`
}

type mFixture struct {
	File      string    `json:"file"`
	Datagrams []mExpect `json:"datagrams"`
}

type mManifest struct {
	Fixtures []mFixture `json:"fixtures"`
}

func keyString(k mKey) string {
	a := netip.MustParseAddr(k.Src)
	d := netip.MustParseAddr(k.Dst)
	return fmt.Sprintf("%s>%s p=%d id=0x%04x", a, d, k.Proto, uint16(k.ID))
}

// verifyManifest checks the report against the manifest entry matching
// pcapPath's base name, and asserts that SQLite holds no buffered
// fragment rows at the end (resource reclamation).
func verifyManifest(manifestPath, pcapPath string, rep *replay.Report, st *store.SQLiteStore, runID string) (bool, error) {
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		return false, err
	}
	var man mManifest
	if err := json.Unmarshal(raw, &man); err != nil {
		return false, err
	}
	name := filepath.Base(pcapPath)
	var fx *mFixture
	for i := range man.Fixtures {
		if man.Fixtures[i].File == name {
			fx = &man.Fixtures[i]
			break
		}
	}
	if fx == nil {
		return false, fmt.Errorf("manifest has no entry for %s", name)
	}
	fmt.Printf("== verifying run_id=%s fixture=%s (go %s, %d packets, %d fragments) ==\n",
		runID, name, rep.GoVersion, rep.Packets, rep.Fragments)

	completed := map[string]replay.CompletedRecord{}
	for _, c := range rep.Completed {
		completed[c.Key] = c
	}
	expired := map[string]bool{}
	for _, k := range rep.Expired {
		expired[k] = true
	}
	// The first rejecting fragment carries the protocol reason; later
	// arrivals are reported as poisoned-group drops.
	rejectReason := map[string]string{}
	for _, in := range rep.Inserts {
		if in.Outcome == "rejected" && rejectReason[in.Key] == "" {
			rejectReason[in.Key] = in.Reason
		}
	}

	pass, total := 0, 0
	check := func(ok bool, format string, args ...any) {
		total++
		if ok {
			pass++
		}
		tag := "FAIL"
		if ok {
			tag = "PASS"
		}
		fmt.Printf("%s  %s\n", tag, fmt.Sprintf(format, args...))
	}

	for _, ex := range fx.Datagrams {
		ks := keyString(ex.Key)
		switch ex.Expect {
		case "completed":
			c, found := completed[ks]
			ok := found
			detail := fmt.Sprintf("completed %s: found=%v", ks, found)
			if found {
				ok = c.SHA256 == ex.SHA256 && c.Length == ex.Length &&
					c.FragCount == ex.Frags && c.Duplicates == ex.Duplicates
				detail = fmt.Sprintf("completed %s: sha_match=%v len=%d/%d frags=%d/%d dup=%d/%d",
					ks, c.SHA256 == ex.SHA256, c.Length, ex.Length, c.FragCount, ex.Frags, c.Duplicates, ex.Duplicates)
			}
			if ok && ex.GoldenFile != "" {
				gb, err := os.ReadFile(filepath.Join(filepath.Dir(manifestPath), ex.GoldenFile))
				if err != nil {
					return false, err
				}
				sum := sha256.Sum256(gb)
				goldenMatch := hex.EncodeToString(sum[:]) == c.SHA256
				ok = goldenMatch
				detail += fmt.Sprintf(" golden_match=%v", goldenMatch)
			}
			check(ok, detail)
		case "rejected":
			got := rejectReason[ks]
			check(got == ex.Reason, "rejected %s: want reason=%q got=%q", ks, ex.Reason, got)
		case "expired":
			check(expired[ks], "expired %s: present=%v", ks, expired[ks])
		default:
			check(false, "unknown expectation %q for %s", ex.Expect, ks)
		}
	}

	// resource reclamation: no buffered fragment rows may remain at end
	rows, err := st.FragCount(runID)
	if err != nil {
		return false, err
	}
	check(rows == 0, "sqlite buffered fragment rows at end = %d (want 0)", rows)

	// failure isolation: keys that are expected only to fail must never
	// leak into output. A key that was expired AND then reused to complete
	// a new datagram (ID reuse after timeout) is expected in both sets.
	hasCompletedExpect := map[string]bool{}
	for _, ex := range fx.Datagrams {
		if ex.Expect == "completed" {
			hasCompletedExpect[keyString(ex.Key)] = true
		}
	}
	leak := 0
	for _, ex := range fx.Datagrams {
		ks := keyString(ex.Key)
		if ex.Expect != "completed" && !hasCompletedExpect[ks] {
			if _, bad := completed[ks]; bad {
				leak++
			}
		}
	}
	check(leak == 0, "rejected/expired keys leaked into completed output: %d", leak)

	fmt.Printf("SUMMARY %s run=%s: %d/%d checks passed\n", name, runID, pass, total)
	return pass == total, nil
}
