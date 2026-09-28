package main

import (
	"fmt"
	"igmpv2timer/internal/store"
	"os"
)

func main() {
	st, err := store.Open(os.Args[1])
	if err != nil {
		panic(err)
	}
	defer st.Close()
	evs, _ := st.Events()
	fmt.Println("journaled input events:", len(evs))
	for _, e := range evs {
		fmt.Printf("  seq=%d at=%d %s %s/%s resp=%q\n", e.Seq, e.At, e.Kind, e.Iface, e.Group, e.ResponseTo)
	}
	d, _ := st.Diagnostics()
	fmt.Println("diagnostics:", len(d))
	for _, x := range d {
		fmt.Printf("  at=%d %-11s %-22s req=%s\n", x.At, x.Verdict, x.Reason, x.RequestID)
	}
	p, _ := st.Emitted()
	fmt.Println("emitted queries:", len(p))
}
