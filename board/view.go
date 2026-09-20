package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

// The palette is a signal box: deep green for what is fine, amber for what is waiting on
// something, red for what is waiting on the operator. Semantic colour only — nothing here is
// decorated, so a glance tells you where to look.
var (
	dim     = lipgloss.NewStyle().Foreground(lipgloss.Color("244"))
	bold    = lipgloss.NewStyle().Bold(true)
	green   = lipgloss.NewStyle().Foreground(lipgloss.Color("35"))
	amber   = lipgloss.NewStyle().Foreground(lipgloss.Color("179"))
	red     = lipgloss.NewStyle().Foreground(lipgloss.Color("174"))
	head    = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("250"))
	colHead = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("244"))
	sel     = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("15")).
		Background(lipgloss.Color("238"))
	warnBar = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("230")).
		Background(lipgloss.Color("124")).Padding(0, 1)

	// Row colours, one per state, so the list sorts itself for the eye before it is read.
	// Working is the brightest thing on screen because it is the thing that is happening;
	// queued sits back; done recedes furthest, since it is there for reassurance, not
	// attention.
	working   = lipgloss.NewStyle().Foreground(lipgloss.Color("252"))
	queued    = lipgloss.NewStyle().Foreground(lipgloss.Color("246"))
	doneStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
)

func ago(ts float64) string {
	if ts == 0 {
		return "-"
	}
	d := time.Since(time.Unix(int64(ts), 0))
	switch {
	case d < 90*time.Second:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	case d < 90*time.Minute:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	default:
		return fmt.Sprintf("%dh%02d", int(d.Hours()), int(d.Minutes())%60)
	}
}

// doneToday counts the finished pile rather than listing all of it: what shipped last week
// is not something to scroll past on the way to what needs you now.
func (m model) doneToday() int {
	n := 0
	cutoff := float64(time.Now().Add(-24 * time.Hour).Unix())
	for _, it := range m.st.Items {
		if it.State == "done" && it.EndedAt > cutoff {
			n++
		}
	}
	return n
}

func (m model) View() string {
	if m.quitting {
		return ""
	}

	// ⚠ Say nothing until the first status read lands. A status read costs about two
	// seconds, most of it asking agy-next for real quota, and until then the model is a
	// zero value — which rendered as "no runners reporting" over "the queue is empty".
	// Both sentences were true of the struct and false of the world, and the board opened
	// on them every single time.
	if !m.loaded && m.err == "" {
		return bold.Render("fleet") + "   " + dim.Render("reading the queue…") + "\n"
	}

	// The limits view replaces the board rather than sitting under it: repeating the
	// capacity header above a screen of per-account detail says the same thing twice.
	if m.limits {
		return m.limitsView()
	}

	width := m.width
	if width < 40 {
		width = 100
	}

	var b strings.Builder
	b.WriteString(bold.Render("fleet") + "   " + m.counts() + "\n")
	if m.st.Halted != "" {
		b.WriteString(warnBar.Render("HALTED  "+m.st.Halted) + "\n")
	}
	if m.err != "" {
		b.WriteString(red.Render("cannot read the queue: "+m.err) + "\n")
	}
	// The limits live here, on the board, not behind a key. What gates the next dispatch
	// is what an account has left, so it belongs where you are already looking.
	b.WriteString(m.strip() + "\n\n")

	// How many lines are left after the header, the limits strip and the footer have taken
	// theirs. Zero means the height is not known yet, and the list then shows everything.
	budget := 0
	if m.height > 0 {
		budget = m.height - strings.Count(b.String(), "\n") - 5
		if budget < 4 {
			budget = 4
		}
	}
	b.WriteString(m.rowsView(width, budget) + "\n")

	if it, ok := m.selected(); ok && it.Note != "" {
		b.WriteString("\n" + dim.Render(trunc(oneLine(it.Note), width-2)) + "\n")
	}

	b.WriteString("\n" + dim.Render(
		"↑↓ move  enter attach  L log  g judge  p park  u limits  t tick  r refresh  q quit"))
	if m.flash != "" {
		b.WriteString(dim.Render("   " + m.flash))
	}
	return b.String()
}

func oneLine(s string) string {
	return strings.Join(strings.Fields(strings.ReplaceAll(s, "\n", " ")), " ")
}

// trunc counts runes, not bytes. The session titles carry em dashes and Bangla, and a byte
// slice through one of those produces a broken glyph rather than an ellipsis.
func trunc(s string, n int) string {
	if n <= 1 {
		return s
	}
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n-1]) + "…"
}
