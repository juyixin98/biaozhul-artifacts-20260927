package netmodel

import (
	"math/big"
)

// Product is one axis-aligned box in the 5-dimensional packet space:
// protocol number × source address × destination address × source port ×
// destination port. All five components are non-empty.
type Product struct {
	Proto Int1D
	Src   Int1D
	Dst   Int1D
	SrcP  Int1D
	DstP  Int1D
}

// Space is a disjoint union of Products of one IP family.
type Space struct {
	Family   Family
	Products []Product
}

// EmptySpace returns the empty space of the family.
func EmptySpace(f Family) Space { return Space{Family: f} }

// UniverseSpace returns the full packet space for a family.
func UniverseSpace(f Family) Space {
	return Space{Family: f, Products: []Product{{
		Proto: Universe1D(BitsProto),
		Src:   Universe1D(AddressBits(f)),
		Dst:   Universe1D(AddressBits(f)),
		SrcP:  Universe1D(BitsPort),
		DstP:  Universe1D(BitsPort),
	}}}
}

// IsEmpty reports whether the union covers no packets.
func (s Space) IsEmpty() bool { return len(s.Products) == 0 }

// Minus returns the set difference s \ o as a disjoint union of Products.
func (s Space) Minus(o Space) Space {
	var out []Product
	for _, a := range s.Products {
		out = append(out, productMinus(a, o.Products)...)
	}
	return Space{Family: s.Family, Products: out}
}

// Intersect returns the set intersection.
func (s Space) Intersect(o Space) Space {
	var out []Product
	for _, a := range s.Products {
		for _, b := range o.Products {
			if p, ok := productIntersect(a, b); ok {
				out = append(out, p)
			}
		}
	}
	return Space{Family: s.Family, Products: out}
}

// Subset reports whether every packet of s is covered by o.
func (s Space) Subset(o Space) bool { return s.Minus(o).IsEmpty() }

// Equals reports set equality.
func (s Space) Equals(o Space) bool { return s.Subset(o) && o.Subset(s) }

func productIntersect(a, b Product) (Product, bool) {
	p := a.Proto.Intersect(b.Proto)
	src := a.Src.Intersect(b.Src)
	dst := a.Dst.Intersect(b.Dst)
	sp := a.SrcP.Intersect(b.SrcP)
	dp := a.DstP.Intersect(b.DstP)
	if p.IsEmpty() || src.IsEmpty() || dst.IsEmpty() || sp.IsEmpty() || dp.IsEmpty() {
		return Product{}, false
	}
	return Product{Proto: p, Src: src, Dst: dst, SrcP: sp, DstP: dp}, true
}

// productMinus removes every box in bs from a by successively splitting on
// each axis. A piece split off on an axis difference is disjoint from the
// hit box on that axis and therefore free of it entirely; only the "inner"
// remainder carries on to the next axis.
func productMinus(a Product, bs []Product) []Product {
	var rec func(Product, []Product) []Product
	rec = func(x Product, rest []Product) []Product {
		var hit *Product
		var others []Product
		for i := range rest {
			if _, ok := productIntersect(x, rest[i]); ok {
				if hit == nil {
					h := rest[i]
					hit = &h
				} else {
					others = append(others, rest[i])
				}
			}
		}
		if hit == nil {
			return []Product{x}
		}
		b := *hit
		var out []Product
		push := func(pieces []Product) {
			for _, pc := range pieces {
				out = append(out, rec(pc, others)...)
			}
		}

		// Axis 1: protocol.
		var difs []Product
		for _, seg := range x.Proto.Minus(b.Proto).Segments() {
			pc := x
			pc.Proto = MustRange1D(x.Proto.Bits(), seg.Lo, seg.Hi)
			difs = append(difs, pc)
		}
		push(difs)
		inner := x
		inner.Proto = x.Proto.Intersect(b.Proto)

		// Axis 2: source address.
		difs = difs[:0]
		for _, seg := range inner.Src.Minus(b.Src).Segments() {
			pc := inner
			pc.Src = MustRange1D(inner.Src.Bits(), seg.Lo, seg.Hi)
			difs = append(difs, pc)
		}
		push(difs)
		inner2 := inner
		inner2.Src = inner.Src.Intersect(b.Src)

		// Axis 3: destination address.
		difs = difs[:0]
		for _, seg := range inner2.Dst.Minus(b.Dst).Segments() {
			pc := inner2
			pc.Dst = MustRange1D(inner2.Dst.Bits(), seg.Lo, seg.Hi)
			difs = append(difs, pc)
		}
		push(difs)
		inner3 := inner2
		inner3.Dst = inner2.Dst.Intersect(b.Dst)

		// Axis 4: source port.
		difs = difs[:0]
		for _, seg := range inner3.SrcP.Minus(b.SrcP).Segments() {
			pc := inner3
			pc.SrcP = MustRange1D(inner3.SrcP.Bits(), seg.Lo, seg.Hi)
			difs = append(difs, pc)
		}
		push(difs)
		inner4 := inner3
		inner4.SrcP = inner3.SrcP.Intersect(b.SrcP)

		// Axis 5: destination port. The leftover inside b on this axis too
		// is x ∩ b, which is the removed intersection itself.
		for _, seg := range inner4.DstP.Minus(b.DstP).Segments() {
			pc := inner4
			pc.DstP = MustRange1D(inner4.DstP.Bits(), seg.Lo, seg.Hi)
			out = append(out, rec(pc, others)...)
		}
		return out
	}
	return rec(a, bs)
}

// Packet is a concrete packet in one family. Port fields are ignored for
// protocols that do not carry ports; such rules collapse the port axes to
// {0} at compile time.
type Packet struct {
	Family  Family
	Proto   int
	SrcAddr *big.Int
	DstAddr *big.Int
	SrcPort int
	DstPort int
}

// Contains reports whether the space contains the concrete packet.
func (s Space) Contains(p Packet) bool {
	pa := Point1D(BitsProto, big.NewInt(int64(p.Proto)))
	sa := Point1D(AddressBits(s.Family), p.SrcAddr)
	da := Point1D(AddressBits(s.Family), p.DstAddr)
	sp := Point1D(BitsPort, big.NewInt(int64(p.SrcPort)))
	dp := Point1D(BitsPort, big.NewInt(int64(p.DstPort)))
	for _, pr := range s.Products {
		if !pr.Proto.Intersect(pa).IsEmpty() &&
			!pr.Src.Intersect(sa).IsEmpty() &&
			!pr.Dst.Intersect(da).IsEmpty() &&
			!pr.SrcP.Intersect(sp).IsEmpty() &&
			!pr.DstP.Intersect(dp).IsEmpty() {
			return true
		}
	}
	return false
}

// Witness returns one concrete packet inside the space (minimal value on
// each axis), or false for an empty space.
func (s Space) Witness() (Packet, bool) {
	if len(s.Products) == 0 {
		return Packet{}, false
	}
	p := s.Products[0]
	return Packet{
		Family:  s.Family,
		Proto:   int(p.Proto.Min().Int64()),
		SrcAddr: p.Src.Min(),
		DstAddr: p.Dst.Min(),
		SrcPort: int(p.SrcP.Min().Int64()),
		DstPort: int(p.DstP.Min().Int64()),
	}, true
}
