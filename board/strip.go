package main

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// The limits, on the board itself rather than behind a key: what gates the next dispatch is
// what an account has left, so it belongs where you are already looking.
//
// One small table per runner, because their buckets genuinely differ — agy meters Gemini
// and third-party models separately, claude has two time windows, cursor has spend and a
// permission. Forcing them into shared columns would leave most cells blank, which reads
// worse than three narrow tables.
//
// ⚠ Colour marks what needs attention, not everything. An earlier version drew every figure
// green, a row of 100%s included, and the eye had nowhere to land: the one account actually
// draining looked exactly like the three that were full. Full is dim now.

// short is the column head for a bucket. The full label belongs in the `u` view; here every
// account has to fit on one line.
func short(id, label string) string {
	switch id {
	case "gemini-5h":
		return "gem 5h"
	case "gemini-weekly":
		return "gem wk"
	case "3p-5h":
		return "3p 5h"
	case "3p-weekly":
		return "3p wk"
	case "five_hour":
		return "5h"
	case "seven_day":
		return "7d"
	case "included":
		return "included"
	case "auto":
		return "auto"
	case "api":
		return "api"
	case "on_demand":
		return "on-dem"
	}
	if label != "" {
		return label
	}
	return id
}

// value is what a bucket reads, and how loudly. Full is quiet on purpose.
func value(q bucket) (string, lipgloss.Style) {
	if q.Remaining == nil {
		return lead(q.Brief), dim
	}
	pct := int(*q.Remaining*100 + 0.5)
	text := fmt.Sprintf("%d%%", pct)
	switch {
	case pct < 15:
		return text, red
	case pct < 40:
		return text, amber
	case pct < 100:
		return text, green
	default:
		return text, dim
	}
}

// soonest is the one reset worth printing: the emptiest bucket that is not already full.
// A full tank's refill time tells you nothing, and four of them per row is how the line
// became a wall in the first place.
func soonest(a account) string {
	best := -1
	for i := range a.Quota {
		q := a.Quota[i]
		if q.Remaining == nil || *q.Remaining >= 1 || q.ResetsAt == nil {
			continue
		}
		if best < 0 || *q.Remaining < *a.Quota[best].Remaining {
			best = i
		}
	}
	if best < 0 {
		return ""
	}
	q := a.Quota[best]
	return short(q.ID, q.Label) + " " + until(q.ResetsAt)
}

func (m model) strip() string {
	if len(m.st.Runners) == 0 {
		return dim.Render("  no runners reporting")
	}

	width := m.width
	if width < 40 {
		width = 100
	}

	// ⚠ One layout, computed once, used by both the heading and the rows. Working them out
	// separately is what produced "main2/2" and a heading whose columns sat half a word
	// left of the figures under it.
	const indent, slotW, gap = 4, 5, 3
	labelW := 4
	for _, r := range m.st.Runners {
		for _, a := range r.Accounts {
			if a.Name != "default" && len(a.Name) > labelW {
				labelW = len(a.Name)
			}
		}
	}
	valuesAt := indent + labelW + 1 + slotW + gap

	var out []string
	for _, r := range m.st.Runners {
		if len(r.Accounts) == 0 {
			continue
		}

		// Column widths come from the widest thing that must sit in them, so the figures
		// line up down the page and can be scanned without being read.
		cols := r.Accounts[0].Quota
		widths := make([]int, len(cols))
		for i, q := range cols {
			widths[i] = len(short(q.ID, q.Label))
		}
		for _, a := range r.Accounts {
			for i, q := range a.Quota {
				if i < len(widths) {
					if text, _ := value(q); len(text) > widths[i] {
						widths[i] = len(text)
					}
				}
			}
		}

		// The runner's name sits left; its bucket names appear once, above the figures.
		title := "  " + head.Render(r.Name)
		pad := valuesAt - 2 - len(r.Name)
		if pad < 1 {
			pad = 1
		}
		if len(cols) > 0 {
			var heads []string
			for i, q := range cols {
				heads = append(heads, fmt.Sprintf("%*s", widths[i], short(q.ID, q.Label)))
			}
			title += strings.Repeat(" ", pad) + dim.Render(strings.Join(heads, "  "))
		}
		out = append(out, title)

		for _, a := range r.Accounts {
			label := a.Name
			if a.Name == "default" {
				label = ""
			}
			slots := fmt.Sprintf("%*s", slotW, fmt.Sprintf("%d/%d", a.Running, a.Ceiling))
			slotStyle := dim
			switch {
			case a.Ceiling == 0:
				slotStyle = red
			case a.Running >= a.Ceiling:
				slotStyle = amber
			}
			prefix := strings.Repeat(" ", indent) +
				fmt.Sprintf("%-*s", labelW, label) + " " +
				slotStyle.Render(slots) + strings.Repeat(" ", gap)

			// A whole row of question marks tells you nothing. When an account reports no
			// usage at all, say that in words where the figures would have been.
			// ⚠ Only a bucket the runner marked unmeasurable, not merely one without a
			// fraction. Cursor's readings have no fraction either and are perfectly
			// informative; catching those here blanked three useful columns.
			blind := len(a.Quota) > 0
			for _, q := range a.Quota {
				if q.Brief != "?" {
					blind = false
					break
				}
			}
			if blind {
				out = append(out, prefix+dim.Render(trunc(a.Quota[0].Detail, width-valuesAt-2)))
				continue
			}

			if len(a.Quota) == 0 {
				why := r.Reason
				if why == "" {
					why = a.Detail
				}
				if why == "" {
					why = "no usage reported"
				}
				out = append(out, prefix+dim.Render(trunc(why, width-valuesAt-2)))
				continue
			}

			var cells []string
			for i, q := range a.Quota {
				w := 6
				if i < len(widths) {
					w = widths[i]
				}
				text, st := value(q)
				cells = append(cells, st.Render(fmt.Sprintf("%*s", w, text)))
			}
			line := prefix + strings.Join(cells, "  ")
			if note := soonest(a); note != "" {
				line += dim.Render("   " + note)
			}
			out = append(out, line)
		}
		out = append(out, "")
	}
	return strings.TrimRight(strings.Join(out, "\n"), "\n")
}

// lead takes the runner's own short form. Deriving it from the full sentence was tried and
// produced "auto pro plan," and "api off —": a first-word heuristic cannot know where the
// useful part of a sentence ends, so the reader says which part it is.
func lead(brief string) string {
	if strings.TrimSpace(brief) == "" {
		return "—"
	}
	return strings.TrimSpace(brief)
}
