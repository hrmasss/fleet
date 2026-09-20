package main

import (
	"fmt"
	"strings"
	"time"
)

// until says how long before an allowance comes back, relative rather than as a clock
// time, because a clock time makes the reader do the subtraction.
func until(ts *float64) string {
	if ts == nil {
		return ""
	}
	d := int(time.Until(time.Unix(int64(*ts), 0)).Seconds())
	if d <= 0 {
		return "due"
	}
	switch {
	case d < 3600:
		return fmt.Sprintf("in %dm", d/60)
	case d < 86400:
		return fmt.Sprintf("in %dh%02d", d/3600, (d%3600)/60)
	default:
		return fmt.Sprintf("in %dd%02dh", d/86400, (d%86400)/3600)
	}
}

// limitsView takes the whole screen, because thirteen buckets across five accounts do not
// fit in a header.
//
// Every runner meters differently: agy has a Gemini pair and a Claude-and-GPT pair, claude
// has a five-hour and a seven-day window, and cursor meters included, auto and api and
// exposes none of it. A runner that reports nothing says so in words — an empty reading and
// a full tank must never look the same.
func (m model) limitsView() string {
	var b strings.Builder
	b.WriteString(bold.Render("fleet limits") + dim.Render("   what is left, and when it returns"))
	b.WriteString("\n\n")

	for _, r := range m.st.Runners {
		for _, a := range r.Accounts {
			name := r.Name
			if a.Name != "default" {
				name = r.Name + "/" + a.Name
			}
			b.WriteString(head.Render(name))
			b.WriteString(dim.Render(fmt.Sprintf("   %d/%d running", a.Running, a.Ceiling)))
			b.WriteString("\n")

			if len(a.Quota) == 0 {
				why := r.Reason
				if why == "" {
					why = a.Detail
				}
				if why == "" {
					why = "nothing reported"
				}
				b.WriteString("  " + dim.Render(why) + "\n\n")
				continue
			}

			group := ""
			for _, q := range a.Quota {
				if q.Group != "" && q.Group != group {
					group = q.Group
					b.WriteString("  " + dim.Render(group) + "\n")
				}
				b.WriteString(bucketLine(q))
			}
			b.WriteString("\n")
		}
	}
	b.WriteString(dim.Render("u  back to the board    r  refresh    q  quit"))
	return b.String()
}

func bucketLine(q bucket) string {
	pct := -1
	if q.Remaining != nil {
		pct = int(*q.Remaining*100 + 0.5)
	}

	// A window with no figure says so. Claude Code only reports its five-hour usage once
	// there is some, and a bare "?" reads as a bug rather than as an idle account.
	cell, bar, note := "   ?", "          ", "not reported yet"
	style := dim
	if pct >= 0 {
		cell = fmt.Sprintf("%3d%%", pct)
		filled := pct / 10
		if filled > 10 {
			filled = 10
		}
		bar = strings.Repeat("█", filled) + strings.Repeat("·", 10-filled)
		switch {
		case pct < 15:
			style = red
		case pct < 40:
			style = amber
		default:
			style = green
		}
		note = until(q.ResetsAt)
	}
	return fmt.Sprintf("    %-20s %s  %s  %s\n",
		q.Label, style.Render(cell), style.Render(bar), dim.Render(note))
}
