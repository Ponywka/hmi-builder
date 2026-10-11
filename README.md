# USART HMI project builder

Stand-alone tools that turn a *portable* USART HMI project (pages as JSON, pictures as PNG, fonts, animations as PNG
frames, see `docs/PORTABLE_FORMAT.md`) into a `.HMI` file, and back. Python only, no vendor files. This repository
contains no display project.

- `tools/` — parser, builder, encoders, tests (`python -m unittest discover -s tools/tests`).
- `docs/PORTABLE_FORMAT.md` — project format description (also written for an LLM).
- `tools/hmi_emulator.py` — emulator of the display: runs a project directory, UART over a pty/TCP, browser UI (`docs/EMULATOR.md`).
- `action.yml` — GitHub Action that builds a project from another repository.

## Use from a project repository

    - uses: actions/checkout@v4
    - uses: ponywka/hmi-builder@main
      with:
        project-dir: display        # folder that contains project.json
        output: out/display.HMI

The step validates the project, builds `out/display.HMI`, checks it and uploads it (with its sha256) as an artifact.

## Locally

    pip install Pillow fonttools
    python tools/hmi_project.py unpack --portable SOURCE.HMI project-dir
    python tools/hmi_project.py validate project-dir --config project.json
    python tools/hmi_project.py pack project-dir out.HMI --config project.json

`.tft` firmware is not built here: only the USART HMI editor can produce it (File -> "Output production file"),
so open the built `.HMI` there for that last step.

## License

MIT, see `LICENSE`. This is an unofficial tool and is not affiliated with the maker of USART HMI. The MIT license
covers the code in this repository; data derived from the editor's own tables (`tools/hmi_schema.json`, the component
reference in `docs/PORTABLE_FORMAT.md`) and the test vectors in `tools/tests/fixtures/` describe the vendor's file
formats and are not covered by it.
