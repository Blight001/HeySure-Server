"""Desktop dynamic-MCP factory history.

Windows is a runner: it has no built-in tools. Desktop MCP exists only as
server-owned dynamic tools in the user workspace, authored in the console or
via ``device+mcp.manage``. This package no longer seeds a factory catalog.

It only remembers fingerprints of older factory files so ``seed_defaults`` can
tombstone untouched copies and leave user-authored tools alone.
"""

from typing import Any, Dict, List, Set

# Revisions of the Python factory defaults shipped before the Windows runtime
# moved to PowerShell.
LEGACY_PYTHON_DEFAULT_REVISIONS = {
    "clipboard.get": "476fd13cfa0b6da890867828e62aecbb99f091546f5f23c639f9655c64398ce4",
    "clipboard.set": "57c3569e48bae468c57e8145baa1dec2df672a2a5a2d1468e3e1643b8729f4a4",
    "display.box": "a1ad87dce58a7766aa7eacfc7d799f62d912418651261c4770d0ee0a23061b3a",
    "fs.list": "c3193153bce09509b9066b5c775f9a0954b3abb5488693d20b167a31fee92360",
    "fs.read": "1223f05231e41eb66b031a4feff794c2d37b2b609b3670a85055e11c2f7b264a",
    "fs.write": "4ff31c095af563197a23719fc894a2dc2e16b68f20e4acbc14f9560b96881871",
    "git.diff": "308ce0723354ff7d5dbd9dbf213f62ea3f0d9a2005a0cf6267d67e0bbaaf3e32",
    "keyboard.press": "771e0dec33868ba352357ad1d698301241b22a88d9787a444d7489075dae8c10",
    "keyboard.type": "377cd392696b072bb5f8b0d6db9cf863fc4eaa9ddae10463c9a1f4d001f9c36d",
    "mouse.click": "e38c76f56e06230f0a374ba33ae401d5cb31abde0b88d6e806a0fcf23e0d1832",
    "mouse.double_click": "c024f6400ba03b8d1280f829e827e2136de532e036eaf12eeb23845cd49bb967",
    "mouse.drag": "5b75eb5166618ae157009e1e3b284f887bea41107d6b5f467202822e71c52baf",
    "mouse.move": "971d5646f6b47e82a86a26bd5e7b2c1bb522234d16d26b2eb50da592f5f2a46e",
    "mouse.right_click": "8749719ba3f9d91b1ad73af40aa7f25dbd5443e27781a053ed725b6ca8e8f0d0",
    "mouse.scroll": "fbf6e9c415dd247c0d76ccd9778f80693490b1fdb7c8b73a7cc37e5f16437857",
    "process.kill": "62688c4221488bfcc1fe9139495af580f90a2851f65fe2b145f7a903f59ae337",
    "process.list": "d2590902bfbad62239c25fb67e0113fe82183e7c98dda68f782f2e4b31935b8e",
    "screen.capture": "54623def419b41da26775f7cf2b01e567791fff74aac33f0d3dbc40bb36eddca",
    "screen.capture_region": "858435a26038197de7ce9e9753ad6d6ae9bc6880f11df91e666ac584f5edd403",
    "screen.info": "e4164d26ec156c674be878c0e14720c715299a85f26d38a8f05aafde9774bec4",
    "speech.speak": "5bda659e24e167fa9b3a14efb940b081e9bad86d69d198ba72274e85a41a6490",
    "text.input": "594f5e3d9668d457f1baac88416e4ea23685bc9e415b6572b8b588ca311e0ad2",
    "ui.click": "0e6c5404ec8f3957a008ad3b903a26695eeeaf991751b5fcc4d799368bbff0ce",
    "ui.inspect": "d0452fea1dc8b323280369c0f471e687bf0cdef715062ede0f901d415b67c870",
    "vision.capture": "cc5cb46e1699f858d5af21adedda132ca00f1d052dc2fce7f40803f654cd2373",
    "vision.capture_mouse": "7ab1bdf4d5ee313bf002355cbaf7772177f5baf25d8c132254596b0ff028b369",
    "window.close": "50149636aa4df0f8be72ad35a5cd0a5107b7efd97723d2d71e1e48a3cd533a57",
    "window.focus": "288edcfd948c044eaae70354759ee1a7ceb19657099bf29427f64a5b110f553a",
    "window.list": "cff6e0db3d4a0f8e7ee9ab7e8b84b661f7c96cb857e590fecb320583da3d6d6a",
}

# Untouched PowerShell factory files from the pre-dynamic-only catalog,
# including the short-lived five-tool grouped set.
LEGACY_POWERSHELL_DEFAULT_REVISIONS = {
    "shell.run": "346bd122a0c6ce4b7229d7fa09a01e635688f5b10b15dad23e9174a510f9652b",
    "keyboard.type": "3cc7ca78b980f9eb2d04c732ee8ae9c4e3e0dc3c28f41feb1bae853edc14484e",
    "keyboard.press": "b553b60f3012b132196da16129f743de4297cbbeb79687d69af4b6b7149bacd5",
    "mouse.move": "603eb6232de100572a3b27dccc7730a9a14515e1e7bfb0bf2069e8bb2985f03e",
    "mouse.click": "45e12e06c14754750b755598ad80a03a3ed3c71139ce8c712718bfc48ae7d462",
    "mouse.double_click": "55be2e309b3f2f54ceedce376767b531c764a232ee91ae9107a859131b921faa",
    "mouse.right_click": "3c075093eac39f45ffcbafff2137522c05f0cf6a544c0a5756341ef711cd8f0b",
    "mouse.scroll": "2ebf460bfecc5c5b38678928bc72674751029633c40bd991aa3a325d53620937",
    "mouse.drag": "36ef8e810973b253803026612afb3da900a52bded33103578733b9e9dd5d48f3",
    "clipboard.get": "56d01f57c6c0c0f3bba330e56b10c839daf2c6df5c8dc0e35150bc5bfdce59e9",
    "clipboard.set": "164adec982ee39d37987284a6fcde1d1b6d68fa2971687ec091d8dbd937ccacc",
    "process.list": "1f3fe5b4c3f5b64936548ad8e471e6486ee36009d7e94d3802a49bd851a1a25e",
    "process.kill": "69696e9e2c80ed69e4ef66f0bcfac619a326a492d5740d5b6b98f1a76d94f735",
    "text.input": "37a89a2aae14ddd14aac826518af32ca9307fd76eebf116d27637dd9eb201dcc",
    "fs.list": "89a3c7e305fa03f23fa2f7f6eb467cb159daec44304cc68fc26906ba9b68fbe6",
    "fs.read": "06e4ddbdf649603c76b017ae36f95347eaca8bd7bed896e9c681d34b361ebe4c",
    "fs.write": "44a5d51c7d4cf2313a1fe74f1465e16ed911c3abaae878ff0227e30d6b7da7da",
    "git.diff": "9475a6107a760a7aa46c3cb64881b6afe9122d35dc08540030be2e526673878e",
    "display.box": "45687fae7daf866974a1393656522e30e79b14b4ae32874b2ad3e6fe0e35b6f2",
    "screen.capture": "ebae164e46e006492485353d7746673595bad9183ae0d22eea97b250d60ba1a2",
    "screen.capture_region": "12e37127cee18c40b01d81487575d27f6130a402ac5b0b873555642fc8319755",
    "screen.info": "a9fb610be2d1f6e7759da7cd365bda084e7d8096ca0a33602c9a859698fd9f4b",
    "vision.capture": "8bea7e721025b168f546a0350b58d779b50955e86333e32317096c369c6ed472",
    "vision.capture_mouse": "997b33994e3c1e4285b6735049a48eb9f05267c58f502862fd63c2fe1be39c12",
    "window.list": "e6d5ea144a181eb78b2b2caebbbb9527acb854ce5567de9e3ebd2b8d2930ca8c",
    "window.focus": "804aa32097fdb895a7a00af5ff7afb8562771f41fa058767085f82b2385d91dd",
    "window.close": "77c0bfa1eac0f24c7ccacf6b2871d958e0d3c5d2af0285278791d74162b22057",
    "ui.inspect": "6832041ea53db11ed32aa6ebdf9ee3f24e199a1f309862d0c6c639c93e55dd99",
    "ui.click": "10d34e480a79e55819b7598b23e7365b6c3ff3c5e9080f67f04483accf447c36",
    "speech.speak": "ccc15f53867ac5f7706f2066fa5a3f85b8175d449b84a2994a2f4b4afa8121cc",
    "run_command": "d651f585d2175414b1db6b8b45e9cac858ee3c21a7a388b583f3cc4c4e730a4a",
    "desktop_observe": "d54de81905f9f1c278de9a28d6f449b997a40f3eda4e241f7778db72e06d2e2a",
    "desktop_screenshot": "d601753239c70913238952263d0301e662da72d74a9e4683ec8e9ce4d9d00f59",
    "desktop_action": "0e6ed00ed73d87cb20cb494f3ca7dbd80182e730bdb5c6af4bf9665ff47315c6",
    "clipboard": "943d93b1f4f5fd2bdce973cc0228b3f66921d4f586303b80826ae676afb54c12",
}

RETIRED_FACTORY_NAMES = frozenset(LEGACY_POWERSHELL_DEFAULT_REVISIONS) | frozenset(
    LEGACY_PYTHON_DEFAULT_REVISIONS
)


def retired_factory_revisions(name: str) -> Set[str]:
    """Known untouched factory revisions for a retired desktop tool name."""
    revisions: Set[str] = set()
    python_revision = LEGACY_PYTHON_DEFAULT_REVISIONS.get(name)
    powershell_revision = LEGACY_POWERSHELL_DEFAULT_REVISIONS.get(name)
    if python_revision:
        revisions.add(python_revision)
    if powershell_revision:
        revisions.add(powershell_revision)
    return revisions


def load_default_tools() -> List[Dict[str, Any]]:
    """Desktop no longer ships a factory catalog."""
    return []
