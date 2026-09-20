package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

// Top down, grouped by what needs you first, because that is the order you read in. The
// kanban columns it replaces looked tidy and were the wrong shape: four columns of a
// terminal's width leaves about twenty characters each, which is not enough for a session
// id and a runner, let alone a preview of what the thing is doing.
//
// One row per session, and the preview is the session's own title. Grouped, and each group
// is skipped entirely when empty rather than printing a heading over nothing.
var groups = []struct {
	title  string
	states []string
}{
	{"Needs you", []string{"needs_you", "parked"}},
	{"Working", []string{"running"}},
	{"Verifying", []string{"verifying"}},
	{"Queued", []string{"queued"}},
	{"Done", []string{"done"}},
}

// spinner frames, braille, because they read as motion at a single character wide.
var spinner = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}

// glyph carries the state in one character, so a column of them scans without reading.
// A working session spins; everything else is still, which is the difference that matters.
func glyph(it item, frame int) string {
	switch it.State {
	case "needs_you", "parked":
		return red.Render("!")
	case "running":
		if it.Activity == "idle" {
			return amber.Render("◦")
		}
		return green.Render(spinner[frame%len(spinner)])
	case "verifying":
		return amber.Render("◇")
	case "done":
		return doneStyle.Render("✓")
	case "queued":
		return dim.Render("·")
	default:
		return dim.Render(" ")
	}
}

// rowStyle is the colour of the line itself. States get their own so the eye can sort the
// list without reading a word of it.
func rowStyle(it item, selected bool) lipgloss.Style {
	if selected {
		return sel
	}
	switch it.State {
	case "needs_you", "parked":
		return red
	case "running":
		if it.Activity == "idle" {
			return amber
		}
		return working
	case "verifying":
		return amber
	case "done":
		return doneStyle
	default:
		return queued
	}
}

// tail is the right-hand column: what this session is waiting on, or how long it has run.
func tail(it item) string {
	when := ago(it.StartedAt)
	if it.StartedAt == 0 {
		when = ago(it.QueuedAt)
	}
	if it.State == "done" && it.EndedAt > 0 {
		when = ago(it.EndedAt)
	}
	return when
}

func (m model) rows() []item {
	var out []item
	for _, g := range groups {
		for _, want := range g.states {
			for _, it := range m.st.Items {
				if it.State == want {
					out = append(out, it)
				}
			}
		}
	}
	return out
}

// rowsView draws the grouped list and returns it. `selected` is an index into m.rows().
func (m model) rowsView(width, budget int) string {
	all := m.rows()
	if len(all) == 0 {
		return dim.Render("  the queue is empty")
	}

	// Widths: the preview takes whatever the fixed columns leave, so the layout holds at a
	// narrow terminal instead of wrapping into itself.
	idW, whereW, ageW := 9, 18, 6
	previewW := width - idW - whereW - ageW - 10
	if previewW < 12 {
		previewW = 12
	}

	// ⚠ The list is cut to fit the terminal rather than allowed to scroll. Overflowing a
	// short window pushed the header — the counts and the limits — off the top, which is
	// the half you actually came to read.
	shown, hidden := 0, 0

	var b strings.Builder
	i := 0
	for _, g := range groups {
		var rows []item
		for _, want := range g.states {
			for _, it := range m.st.Items {
				// ⚠ Done is capped to the last day, matching the count in the header. What
				// shipped last week is not something to scroll past on the way to what needs
				// you now, and the header said "6 done today" over a list of thirty.
				if it.State == want && (want != "done" || recent(it.EndedAt)) {
					rows = append(rows, it)
				}
			}
		}
		if len(rows) == 0 {
			continue
		}
		if budget > 0 && shown+2 > budget {
			hidden += len(rows)
			i += len(rows)
			continue
		}
		b.WriteString(colHead.Render(fmt.Sprintf("%s  %d", g.title, len(rows))) + "\n")
		shown++
		for _, it := range rows {
			if budget > 0 && shown >= budget {
				hidden++
				i++
				continue
			}
			// A queued session has not run yet, so it has no runner — show what it
			// asked for instead of a blank column.
			where := it.Runner
			if where == "" {
				where = it.Wanted
			}
			if it.Account != "" && it.Account != "default" {
				where += "/" + it.Account
			}
			preview := it.Title
			if preview == "" {
				preview = oneLine(it.Note)
			}
			if it.Verdict != "" && (it.State == "needs_you" || it.State == "parked") {
				preview = it.Verdict + " — " + preview
			}
			// The model family, not the whole id: on agy the family is what says which of
			// the account's two allowances this session spends, and a full
			// "claude-opus-4-6-thinking" would take the column on its own.
			if it.Model != "" {
				where += " " + family(it.Model)
			}
			if it.Attempts > 1 {
				where += fmt.Sprintf(" x%d", it.Attempts)
			}

			// ⚠ Build the body unstyled, then attach the glyph. The glyph carries colour,
			// and truncating a string that already holds escape codes counts them as
			// width — which is how the selected row lost its age column entirely.
			body := fmt.Sprintf("%-*s %-*s %-*s %*s",
				idW, trunc(it.Session, idW),
				previewW, trunc(preview, previewW),
				whereW, trunc(where, whereW),
				ageW, tail(it))

			body = rowStyle(it, i == m.row).Render(body)
			b.WriteString(" " + glyph(it, m.frame) + " " + body + "\n")
			shown++
			i++
		}
		b.WriteString("\n")
		shown++
	}
	out := strings.TrimRight(b.String(), "\n")
	if hidden > 0 {
		out += "\n" + dim.Render(fmt.Sprintf("   +%d more", hidden))
	}
	return out
}

// counts is the one-line summary above everything: what needs you, then what is moving.
func (m model) counts() string {
	n := map[string]int{}
	for _, it := range m.st.Items {
		n[it.State]++
	}
	idle := 0
	for _, it := range m.st.Items {
		if it.State == "running" && it.Activity == "idle" {
			idle++
		}
	}
	parts := []string{}
	if k := n["needs_you"] + n["parked"]; k > 0 {
		parts = append(parts, red.Render(fmt.Sprintf("%d need you", k)))
	}
	working := n["running"] - idle
	parts = append(parts, fmt.Sprintf("%d working", working))
	if idle > 0 {
		parts = append(parts, amber.Render(fmt.Sprintf("%d idle", idle)))
	}
	if n["verifying"] > 0 {
		parts = append(parts, fmt.Sprintf("%d verifying", n["verifying"]))
	}
	parts = append(parts, fmt.Sprintf("%d queued", n["queued"]))
	if d := m.doneToday(); d > 0 {
		parts = append(parts, dim.Render(fmt.Sprintf("%d done today", d)))
	}
	return strings.Join(parts, dim.Render(" · "))
}

// family is the part of a model id that says which pool it spends: claude, gpt, gemini.
// Everything after the first dash is a version and a size, and neither changes the answer.
func family(model string) string {
	if i := strings.IndexByte(model, '-'); i > 0 {
		return model[:i]
	}
	return model
}

// recent is the window the Done group shows. Anything older is still in the ledger and
// still in `fleet status`; it has just stopped competing for the screen.
func recent(ts float64) bool {
	return ts > float64(time.Now().Add(-24*time.Hour).Unix())
}
