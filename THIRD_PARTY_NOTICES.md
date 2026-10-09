# Third-party notices

The root [MIT License](LICENSE) covers the new integration code and
documentation contributed to wx-qq-mcp. It does not replace the licenses of
third-party material, installed dependencies, external executables, or model
weights. Copyright notices remain with their respective authors.

This source distribution contains no external reader executable, native DLL,
FFmpeg build, Python wheel or virtual environment, speech model, application
database, key, or user media. Those components are obtained separately where
needed. The license files below record attribution; their presence does not
mean that the corresponding binary is bundled.

## WeChat reader interface

- Upstream: [r266-tech/wechat-local-mcp](https://github.com/r266-tech/wechat-local-mcp).
- Copyright (c) 2026 R266 Tech.
- License: MIT; original text preserved in
  [licenses/R266-Tech-MIT.txt](licenses/R266-Tech-MIT.txt).
- Included interface material: `unified_mcp/wechat_legacy_tools.json`, captured
  from the upstream v1.5.4 MCP tool definitions. Any additional retained
  upstream tool-schema snapshot has the same attribution. Integration changes
  do not remove that attribution.
- The original `wx-mcp` / `wechat-cli` reader and its Go implementation are not
  distributed in this repository. Configure a separately acquired compatible
  reader. A retained upstream release supplied the license text; the upstream
  repository returned HTTP 451 during the publication audit. This repository
  does not mirror that reader or claim to be its official release.

## QQ integration and references

`qq_mcp_server.py`, `setup_qq_mcp_key.py`, and the QQ modules under
`unified_mcp/` are local integration modules generalized for this project.
Public descriptions of the database format and key representation informed
the integration. This attribution does not assert exclusive ownership of
those formats or of all underlying techniques.

### qq-nt-export

- Reference project: [q957506633/qq-nt-export](https://github.com/q957506633/qq-nt-export).
- Copyright (c) 2026 qq-nt-export contributors.
- License: [upstream MIT](https://github.com/q957506633/qq-nt-export/blob/main/LICENSE);
  original text preserved in
  [licenses/qq-nt-export-MIT.txt](licenses/qq-nt-export-MIT.txt).
- Its raw-key-plus-salt handling and NTQQ column semantics were consulted.
  The notice is retained for that provenance and any adaptations. Its
  standalone export application and binaries are not bundled.

### NTQQ SQLite extension

- External dependency: `sqlite_ext_ntqq_db`, part of
  [artiga033/ntdb_unwrap](https://github.com/artiga033/ntdb_unwrap/tree/main/sqlite_extension).
- Copyright (c) 2025 artiga033.
- License: [upstream MIT](https://github.com/artiga033/ntdb_unwrap/blob/main/LICENSE);
  original text preserved in
  [licenses/ntdb_unwrap-MIT.txt](licenses/ntdb_unwrap-MIT.txt).
- The SQLite extension is loaded from a separately installed DLL. No DLL or
  extension source is bundled here. Obtain builds from the
  [upstream release page](https://github.com/artiga033/ntdb_unwrap/releases)
  or build the upstream source, retaining its notices. Its Rust SQLite binding
  and any other build dependencies retain their own licenses.

## Direct Python dependencies

The table records the direct requirements used by this integration. License
labels were checked against the metadata for the specified versions on PyPI;
the linked upstream texts and the notices installed with each package govern
those packages. This table is not a replacement for wheel contents or a claim
that every transitive or native dependency uses the same license.

| Distribution | Version | Declared license | Upstream license or source |
| --- | --- | --- | --- |
| `mcp` | 1.30.0 | MIT | [Python SDK license](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE) |
| `sqlcipher3-wheels` | 0.5.7 | zlib/libpng | [sqlcipher3 license](https://github.com/laggykiller/sqlcipher3/blob/master/LICENSE) |
| `Pillow` | 12.2.0 | MIT-CMU | [Pillow license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| `cryptography` | 49.0.0 | Apache-2.0 OR BSD-3-Clause | [cryptography licenses](https://github.com/pyca/cryptography/blob/main/LICENSE) |
| `pycryptodome` | 3.23.0 | BSD and public-domain components | [PyCryptodome license](https://github.com/Legrandin/pycryptodome/blob/master/LICENSE.rst) |
| `faster-whisper` | 1.2.1 | MIT | [faster-whisper license](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE) |
| `av` (PyAV) | 18.1.0 | BSD-3-Clause | [PyAV license](https://github.com/PyAV-Org/PyAV/blob/main/LICENSE.txt) |
| `ctranslate2` | 4.8.2 | MIT | [CTranslate2 license](https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE) |
| `silk-python` | 0.2.8 | BSD, as declared by its publisher | [pysilk source and packaging declaration](https://github.com/synodriver/pysilk/tree/v0.2) |
| `sherpa-onnx` | 1.13.8 | Apache-2.0 | [sherpa-onnx license](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE) |

The SILK binding uses a separate SILK codec source submodule. Its codec notices
are not replaced by the binding's BSD metadata. SQLCipher wheels and other
native wheels can include libraries with their own notices. This repository
uses package installation rather than redistributing any such wheel.

## Other external runtime components

- **Tencent WCDB:** the external WeChat reader may load `libWCDB.dll`.
  [Tencent's WCDB license](https://github.com/Tencent/wcdb/blob/master/LICENSE)
  identifies BSD-3-Clause terms and additional bundled-component notices.
  No WCDB DLL or source is included here; the license of a particular supplied
  build must accompany that build.
- **FFmpeg / ffprobe:** optional local video inspection and decoding programs.
  [FFmpeg's license explanation](https://ffmpeg.org/legal.html) distinguishes
  LGPL builds from builds that include GPL components. The exact build and
  enabled components determine the applicable terms. No FFmpeg binary is
  included here. PyAV's BSD license does not relicense FFmpeg libraries.
- **Windows OCR and Windows APIs:** the OCR worker calls the installed
  `Windows.Media.Ocr` system API; QQ key protection uses Windows DPAPI. These
  operating-system components are provided by Microsoft under the applicable
  Windows terms. No Microsoft system component is redistributed.
- **Python:** users install their own interpreter; Python's
  [PSF license and bundled-component notices](https://docs.python.org/3/license.html)
  apply to that interpreter. It is not bundled here.

## Speech model weights

Speech models are separate optional downloads. The code's license does not
grant rights to model weights, and converting weights to ONNX or CTranslate2
does not automatically change their license.

- **Whisper Small / faster-whisper-small:**
  [Systran's converted model card](https://huggingface.co/Systran/faster-whisper-small/blob/main/README.md)
  declares MIT and identifies the conversion from OpenAI Whisper Small.
  [OpenAI Whisper's license](https://github.com/openai/whisper/blob/main/LICENSE)
  and the model's own notices remain applicable. No weights are included here.
- **SenseVoiceSmall, including the ONNX/int8 variant:**
  [the official model card](https://huggingface.co/FunAudioLLM/SenseVoiceSmall/blob/main/README.md)
  names a separate `model-license` and links to the
  [FunASR Model Open Source License Agreement](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE).
  That model license must be reviewed for the separately obtained weights.
  [SenseVoice program code](https://github.com/QwenAudio/SenseVoice/blob/main/LICENSE)
  being MIT does not make these weights MIT. The integration can use converted
  weights supplied by the sherpa-onnx community; that conversion does not
  remove the original model's terms. No SenseVoice weights are bundled.

## Attribution and distribution scope

WeChat, QQ, Windows, and the names of upstream projects belong to their
respective owners. This community integration is not an official release or
endorsement by those owners. User databases, media, credentials, and generated
transcripts remain user data and are outside the project's MIT grant.

The files in `licenses/` preserve the original notices for the interface
material and named reference projects. [licenses/SOURCES.json](licenses/SOURCES.json)
records their retrieval sources and SHA-256 digests. Anyone preparing a future
binary bundle must separately preserve the license notices required by the
actual components included in that bundle.
