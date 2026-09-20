.PHONY: install test lint fmt board clean

install:
	uv sync --group dev

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests

board:
	cd board && go build -o fleet-board .

clean:
	rm -rf .pytest_cache .ruff_cache board/fleet-board board/fleet-board.exe

# The board is the only Go, and it only ever reads `fleet status --json`.
# Built here and shipped to the box, so the box needs no Go toolchain.
board-linux:
	cd board && GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o fleet-board-linux .

ship-board: board-linux
	scp board/fleet-board-linux the box:~/.local/bin/fleet-board
	ssh the box 'chmod +x ~/.local/bin/fleet-board'
