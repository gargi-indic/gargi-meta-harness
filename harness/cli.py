"""CLI entrypoint for Gargi Harness."""

import typer

from harness import __version__

app = typer.Typer(help="Gargi Harness CLI")


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        help="Print the version.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """Gargi Harness."""
    pass


@app.command()
def status() -> None:
    """Print the harness status."""
    print("not configured")
