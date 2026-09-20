// Command fleet-board is a read-only view of the dispatch queue.
//
// It owns nothing. Every fact on screen comes from `fleet status --json` and every key
// shells out to a fleet verb, so the board can be rewritten or thrown away without
// touching anything that dispatches work. That contract is the reason this is allowed to
// be a second language at all.
//
// Run it on the box, where tmux lives:
//
//	ssh -t the box fleet-board
//
// Set FLEET_CMD to point it elsewhere (for example "ssh the box fleet"), but attaching to
// a pane only works where the panes are.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// ---- what `fleet status --json` hands back ----

// bucket is one metered allowance. Every runner meters differently — agy has a Gemini
// pair and a Claude-and-GPT pair, claude has a five-hour and a seven-day window, cursor
// meters included/auto/api and exposes none of it — so they are normalised upstream to a
// name, a fraction left and a reset time.
type bucket struct {
	ID        string   `json:"id"`
	Label     string   `json:"label"`
	Group     string   `json:"group"`
	Remaining *float64 `json:"remaining"`
	ResetsAt  *float64 `json:"resets_at"`
	Detail    string   `json:"detail"`
	Brief     string   `json:"brief"`
}

type account struct {
	Name    string   `json:"name"`
	Ceiling int      `json:"ceiling"`
	Running int      `json:"running"`
	Ours    int      `json:"ours"`
	Detail  string   `json:"detail"`
	Quota   []bucket `json:"quota"`
}

type runner struct {
	Name     string    `json:"name"`
	Reason   string    `json:"reason"`
	Accounts []account `json:"accounts"`
}

type item struct {
	Session   string  `json:"session"`
	Title     string  `json:"title"`
	Activity  string  `json:"activity"`
	State     string  `json:"state"`
	Runner    string  `json:"runner"`
	Wanted    string  `json:"wanted"`
	Model     string  `json:"model"`
	Account   string  `json:"account"`
	Attempts  int     `json:"attempts"`
	Verdict   string  `json:"verdict"`
	Note      string  `json:"note"`
	Tmux      string  `json:"tmux"`
	Log       string  `json:"log"`
	QueuedAt  float64 `json:"queued_at"`
	StartedAt float64 `json:"started_at"`
	EndedAt   float64 `json:"ended_at"`
}

type status struct {
	Halted  string   `json:"halted"`
	Runners []runner `json:"runners"`
	Items   []item   `json:"items"`
}

func fleetCmd() []string {
	if v := os.Getenv("FLEET_CMD"); v != "" {
		return strings.Fields(v)
	}
	return []string{"fleet"}
}

func run(args ...string) (string, error) {
	base := fleetCmd()
	c := exec.Command(base[0], append(base[1:], args...)...)
	out, err := c.CombinedOutput()
	return string(out), err
}

// ---- model ----

type tickMsg time.Time
type frameMsg time.Time
type statusMsg struct {
	s   status
	err error
}

type model struct {
	st       status
	loaded   bool
	frame    int
	row      int
	err      string
	flash    string
	limits   bool
	quitting bool
	width    int
	height   int
}

func fetch() tea.Cmd {
	return func() tea.Msg {
		out, err := run("status", "--json")
		if err != nil {
			return statusMsg{err: fmt.Errorf("%v: %s", err, firstLine(out))}
		}
		var s status
		if err := json.Unmarshal([]byte(out), &s); err != nil {
			return statusMsg{err: fmt.Errorf("unreadable status: %v", err)}
		}
		return statusMsg{s: s}
	}
}

// A status read costs about two seconds, most of it asking agy-next for real quota.
// Polling faster than this would keep the box permanently busy answering a board nobody is
// looking at that hard.
const refresh = 5 * time.Second

func tick() tea.Cmd {
	return tea.Tick(refresh, func(t time.Time) tea.Msg { return tickMsg(t) })
}

// The spinner runs on its own clock. Folding it into the five-second refresh would mean
// either a spinner that ticks once every five seconds, which does not read as motion, or a
// status read every hundred milliseconds, which would keep the box busy answering a board.
const framerate = 110 * time.Millisecond

func frame() tea.Cmd {
	return tea.Tick(framerate, func(t time.Time) tea.Msg { return frameMsg(t) })
}

func (m model) Init() tea.Cmd { return tea.Batch(fetch(), tick(), frame()) }

func (m model) selected() (item, bool) {
	rows := m.rows()
	if m.row < 0 || m.row >= len(rows) {
		return item{}, false
	}
	return rows[m.row], true
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil

	case tickMsg:
		return m, tea.Batch(fetch(), tick())

	case frameMsg:
		m.frame++
		return m, frame()

	case statusMsg:
		if msg.err != nil {
			m.err = msg.err.Error()
			return m, nil
		}
		m.err = ""
		m.st = msg.s
		m.loaded = true
		if n := len(m.rows()); m.row >= n {
			m.row = max(0, n-1)
		}
		return m, nil

	case tea.KeyMsg:
		return m.key(msg)
	}
	return m, nil
}

func (m model) key(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "q", "ctrl+c":
		m.quitting = true
		return m, tea.Quit

	case "up", "k":
		m.row = max(0, m.row-1)
	case "down", "j":
		m.row = min(len(m.rows())-1, m.row+1)
	case "home":
		m.row = 0
	case "end", "G":
		m.row = max(0, len(m.rows())-1)

	case "u":
		m.limits = !m.limits

	case "r":
		m.flash = "refreshing"
		return m, fetch()

	case "t":
		// A tick is the one write the board makes, and it is the same one the hourly
		// timer makes. Nothing here can dispatch that the queue would not have anyway.
		out, _ := run("tick")
		m.flash = firstLine(out)
		return m, fetch()

	case "enter":
		if it, ok := m.selected(); ok && it.Tmux != "" {
			return m, attach(it.Tmux)
		}
		m.flash = "nothing to attach to"

	case "L":
		if it, ok := m.selected(); ok {
			return m, pager(fleetCmd(), "log", it.Session, "-n", "200")
		}

	case "g":
		if it, ok := m.selected(); ok {
			m.flash = "judging " + it.Session + "…"
			return m, judge(it.Session)
		}

	case "p":
		if it, ok := m.selected(); ok {
			out, _ := run("park", it.Session, "--note", "parked from the board")
			m.flash = firstLine(out)
			return m, fetch()
		}
	}
	return m, nil
}

// attach hands the terminal to tmux and takes it back when you detach.
func attach(session string) tea.Cmd {
	c := exec.Command("tmux", "attach", "-t", session)
	return tea.ExecProcess(c, func(err error) tea.Msg { return tickMsg(time.Now()) })
}

func pager(base []string, args ...string) tea.Cmd {
	full := strings.Join(append(base, args...), " ")
	c := exec.Command("sh", "-c", full+" | less -R")
	return tea.ExecProcess(c, func(err error) tea.Msg { return tickMsg(time.Now()) })
}

func judge(session string) tea.Cmd {
	return func() tea.Msg {
		out, _ := run("foreman", "--judge", session)
		return statusMsgFromJudge(out)
	}
}

// statusMsgFromJudge re-reads the board after a judgement rather than trying to splice the
// verdict in: the ledger is the truth and the judgement has already been written to it.
func statusMsgFromJudge(out string) tea.Msg {
	_ = out
	s, err := run("status", "--json")
	if err != nil {
		return statusMsg{err: err}
	}
	var st status
	if e := json.Unmarshal([]byte(s), &st); e != nil {
		return statusMsg{err: e}
	}
	return statusMsg{s: st}
}

func main() {
	p := tea.NewProgram(model{}, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "fleet-board:", err)
		os.Exit(1)
	}
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
