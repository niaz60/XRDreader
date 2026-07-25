import argparse

from diffai.xrdreader.version import __version__  # noqa


def main():
    parser = argparse.ArgumentParser(
        prog="diffai.xrdreader",
        description=(
            "Multi-step agentic framework for extracting powder diffraction patterns and associated metadata from "
            "published literature\n\n"
            "For more information, visit: "
            "https://github.com/diffractionai/diffai.xrdreader/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version",
        action="store_true",
        help="Show the program's version number and exit",
    )

    args = parser.parse_args()

    if args.version:
        print(f"diffai.xrdreader {__version__}")
    else:
        # Default behavior when no arguments are given
        parser.print_help()


if __name__ == "__main__":
    main()
