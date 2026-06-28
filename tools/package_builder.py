"""
DBPF v2.1 .package builder for The Sims 4.

Reverse-engineered from S4S output. Builds a .package containing:
- One String Table (STBL v5) resource for pie-menu display names
- One Tuning resource per .xml file in package_src/

Tuning instance ID is read from the s="..." attribute of each XML root.
STBL is built from a tiny dict at the bottom of this file.

Format notes:
- All resources are zlib-compressed (marker 0x5A42).
- disk_size has the high bit set when compressed.
- Header is 96 bytes, fixed DBPF v2.1 layout.
- Index follows resources, all fields per-entry (flags=0).
"""
import os
import struct
import sys
import zlib
import re
from pathlib import Path


# Resource type constants (Sims 4 standard)
TYPE_STBL = 0x220557DA            # String Table
TYPE_TUNING = 0xE882D22F          # Generic XML tuning (interactions, snippets)
TYPE_PIE_MENU_CATEGORY = 0x03E9D964  # PieMenuCategory tuning
TYPE_SIMDATA = 0x545AC67A         # SimData binary companion (read by UI client)

# Magic group that SimData companions for PieMenuCategory tunings use.
# Verified across multiple shipping mods (Basemental Drugs, World Tour);
# at group=0 the UI never finds the SimData and the phone wheel crashes
# with "Failed to locate category info for interaction category...".
SIMDATA_PMC_GROUP = 0xE9D967

# Map an XML `i="..."` attribute to its resource type. Without this the
# packer would stuff PieMenuCategory tunings into the generic tuning
# bucket, and the game would fail to recognise them.
XML_TYPE_BY_KIND = {
    "interaction":          TYPE_TUNING,
    "pie_menu_category":    TYPE_PIE_MENU_CATEGORY,
    # Fallback for any other XML tuning kinds we ship later
}

# Compression marker. We always zlib-compress to match what S4S does.
COMPRESSION_ZLIB = 0x5A42


# ----- PieMenuCategory SimData (UI-client companion) -----
#
# The phone wheel UI is implemented in ActionScript on the client side,
# which reads PieMenuCategory data out of a SimData binary resource that
# rides alongside the XML tuning. Without SimData, the client throws
# "Failed to locate category info for interaction category..." and the
# phone refuses to open.
#
# Sims4Studio normally emits this automatically when you author a tuning.
# Since we hand-roll the .package, we ship a captured 425-byte template
# from a real PieMenuCategory (Basemental:phoneCategory) and patch in our
# own display_name hash and trailing name string. Schema bytes are
# identical across all PieMenuCategory tunings so we don't have to
# regenerate them.
PIE_MENU_CATEGORY_SIMDATA_TEMPLATE = bytes.fromhex(
    "4441544101010000180000000100000070000000010000000000008000000000"
    "70010000f3e143ce580000000d000000380000000c0000000100000000000000"
    "0000000078bf61a50800000000000000ae31a1dcd32edb0e82d8b20000000000"
    "0000000000000000000000000000000000000080000000000000000000000000"
    "00010000ccad21dbc1652002380000000800000007000000bf000000806d6139"
    "120000002000000000000080b30000002c57a95b080000002800000000000080"
    "64000000a85f00920000000000000000000000806b000000296a5da406000000"
    "0800000000000080690000001b7ef1ae13000000100000000000008035000000"
    "b8be3ad2140000000400000000000080610000009e304bda0e00000030000000"
    "000000805f636f6c6c61707369626c65005f646973706c61795f6e616d65005f"
    "646973706c61795f7072696f72697479005f69636f6e005f706172656e74005f"
    "7370656369616c5f63617465676f7279006d6f6f645f6f766572726964657300"
    "5069654d656e7543617465676f727900426173656d656e74616c3a70686f6e65"
    "43617465676f727900"
)
# Patch points within the template, byte-aligned offsets:
#   0x24-0x27  TableInfo.NameHash -- FNV-1 32-bit of lowercased tuning name
#   0x40-0x43  display_name STBL hash (u32 LE)
#   0x44       display_priority (u8 -- low byte, surrounding bytes are 0)
#   0x50-0x57  _icon instance (u64 LE)
#   0x58-0x5B  _icon type (u32 LE)
#   0x5C-0x5F  _icon group (u32 LE)
#   0x190+     null-terminated tuning instance name string
_SD_NAME_HASH_OFFSET = 0x24
_SD_DISPLAY_NAME_OFFSET = 0x40
_SD_DISPLAY_PRIORITY_OFFSET = 0x44
_SD_ICON_INSTANCE_OFFSET = 0x50
_SD_ICON_TYPE_OFFSET = 0x58
_SD_ICON_GROUP_OFFSET = 0x5C
_SD_NAME_STRING_OFFSET = 0x190
_SD_NAME_STRING_REGION = len(PIE_MENU_CATEGORY_SIMDATA_TEMPLATE) - _SD_NAME_STRING_OFFSET

# Icon resource keys in SimData use type 0x00B2D882, NOT the 0x2F7D0004
# that appears in tuning XML. They reference the SAME texture instance,
# but S4S applies a type-code remap when compiling SimData. Verified
# against S4S's export of the in-game phoneCategory_Social:
#   Tuning XML: 2f7d0004:00000000:a343ee5a37889ab0
#   SimData XML: 00B2D882-00000000-A343EE5A37889AB0
_SD_ICON_TYPE = 0x00B2D882

# In-game stock cellphone icon -- exists in base-game data, doesn't depend
# on any mod. Used as default when no override is provided.
_DEFAULT_ICON_GROUP = 0x00000000
_DEFAULT_ICON_INSTANCE = 0x6189CED9570B8609


def fnv1_32_lower(name):
    """FNV-1 32-bit hash of the LOWERCASED ASCII name. This is how the
    Sims 4 SimData TableInfo.NameHash field maps back to the trailing
    instance-name string."""
    h = 0x811C9DC5
    for c in name.lower().encode('ascii'):
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= c
    return h


def build_pie_menu_category_simdata(
    display_name_hash, instance_name, display_priority=1,
    icon_type=_SD_ICON_TYPE,
    icon_group=_DEFAULT_ICON_GROUP,
    icon_instance=_DEFAULT_ICON_INSTANCE,
):
    """Patch the captured template with this category's display_name hash,
    icon ResourceKey, display_priority, and trailing name string (plus the
    recomputed NameHash that maps to it). Returns a 425-byte SimData blob
    ready to pack."""
    data = bytearray(PIE_MENU_CATEGORY_SIMDATA_TEMPLATE)
    struct.pack_into('<I', data, _SD_NAME_HASH_OFFSET, fnv1_32_lower(instance_name))
    struct.pack_into('<I', data, _SD_DISPLAY_NAME_OFFSET, display_name_hash)
    # display_priority is a single byte at 0x44, with the next 3 bytes left zero.
    data[_SD_DISPLAY_PRIORITY_OFFSET] = display_priority & 0xFF
    for i in range(_SD_DISPLAY_PRIORITY_OFFSET + 1, _SD_DISPLAY_PRIORITY_OFFSET + 4):
        data[i] = 0
    struct.pack_into('<Q', data, _SD_ICON_INSTANCE_OFFSET, icon_instance)
    struct.pack_into('<I', data, _SD_ICON_TYPE_OFFSET, icon_type)
    struct.pack_into('<I', data, _SD_ICON_GROUP_OFFSET, icon_group)

    name_bytes = instance_name.encode('utf-8') + b'\x00'
    if len(name_bytes) > _SD_NAME_STRING_REGION:
        raise ValueError(
            f"PieMenuCategory instance name {instance_name!r} is too long for the "
            f"template ({_SD_NAME_STRING_REGION} byte region, "
            f"{len(name_bytes)} bytes needed)."
        )
    for i in range(_SD_NAME_STRING_OFFSET, len(data)):
        data[i] = 0
    data[_SD_NAME_STRING_OFFSET:_SD_NAME_STRING_OFFSET + len(name_bytes)] = name_bytes
    return bytes(data)


def build_stbl_v5(strings):
    """Build a v5 STBL resource body from {key_int: utf8_string}.

    Reverted to the original S4PE-style header layout because the
    "21-byte header + flags byte" variant I reverse-engineered from
    WorldTour/Basemental causes Sims 4 to refuse to open with the
    .package loaded -- it appears the loader validates a specific
    field that our reduced header didn't satisfy.

    With this layout the game LOADS the .package and the interactions
    work, but display_name hashes don't resolve so menu rows show as
    blank rows. That's a known cosmetic issue we'll address separately
    (likely by referencing an existing base-game string hash or by
    matching the metadata bytes that real mods set at offsets 14-20).

    Layout (22-byte header):
      'STBL'            (4 bytes)
      version           (1 byte)   = 0x05
      compressed        (1 byte)   = 0x00
      count             (u64 LE)
      mn_string_length  (u32 LE)   = 0
      total_str_len     (u32 LE)

    Entries (no flags byte):
      key       (u32 LE)
      length    (u16 LE)
      string    (length bytes UTF-8)
    """
    out = bytearray()
    out += b'STBL'
    out += bytes([0x05, 0x00])
    out += struct.pack('<Q', len(strings))
    encoded = [(k, s.encode('utf-8')) for k, s in strings.items()]
    total_len = sum(len(b) for _, b in encoded)
    out += struct.pack('<II', 0, total_len)
    for key, raw in encoded:
        out += struct.pack('<I', key)
        out += struct.pack('<H', len(raw))
        out += raw
    return bytes(out)


def read_tuning_attrs(xml_text):
    """Extract the root <I ...> attributes from a tuning XML.

    Returns (instance_id_int, i_kind_string, tuning_name). tuning_name
    is the `n=` attribute -- needed by the SimData companion for
    PieMenuCategory tunings.
    """
    m = re.search(r'<I\s+([^>]+)>', xml_text)
    if not m:
        raise ValueError("Couldn't find root <I ...> tag in tuning XML")
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    raw = attrs.get('s', '').strip()
    if not raw:
        raise ValueError("Tuning XML root missing s=\"...\" attribute")
    if raw.lower().startswith('0x'):
        instance = int(raw, 16)
    else:
        instance = int(raw)
    kind = attrs.get('i', 'interaction')
    name = attrs.get('n', '')
    return instance, kind, name


def read_display_name_hash(xml_text):
    """For PieMenuCategory XMLs, return the int hash referenced by the
    `_display_name` tunable, or None if the field is absent or unparseable."""
    m = re.search(r'<T\s+n="_display_name"\s*>([^<]+)</T>', xml_text)
    if not m:
        return None
    raw = m.group(1).strip().split('<')[0].strip()  # ignore inline <!--comments-->
    try:
        if raw.lower().startswith('0x'):
            return int(raw, 16)
        return int(raw)
    except ValueError:
        return None


def build_package(resources, out_path):
    """Write a .package file from a list of (type_id, group, instance, body_bytes).

    Each body is zlib-compressed before writing. The high bit on disk_size
    is set to signal "compressed" (matches S4S behavior).
    """
    HEADER_SIZE = 96
    chunks = []      # list of (compressed_bytes, mem_size)
    for (_t, _g, _i, body) in resources:
        compressed = zlib.compress(body, 9)
        chunks.append((compressed, len(body)))

    # Resource data block follows the header
    body_blob = bytearray()
    offsets = []
    cursor = HEADER_SIZE
    for compressed, _mem in chunks:
        offsets.append(cursor)
        body_blob += compressed
        cursor += len(compressed)

    # Build the index
    idx = bytearray()
    idx += struct.pack('<I', 0)  # flags = 0 (all fields explicit per entry)
    for (t, g, i, body), (compressed, mem), offset in zip(resources, chunks, offsets):
        inst_hi = (i >> 32) & 0xFFFFFFFF
        inst_lo = i & 0xFFFFFFFF
        # disk_size with high bit set = compressed
        disk_size_field = len(compressed) | 0x80000000
        idx += struct.pack('<IIIIIIIHH',
                           t,
                           g,
                           inst_hi,
                           inst_lo,
                           offset,
                           disk_size_field,
                           mem,
                           COMPRESSION_ZLIB,
                           0x0001)  # committed flag

    index_offset = HEADER_SIZE + len(body_blob)
    index_size = len(idx)

    # Header (96 bytes total)
    header = bytearray(HEADER_SIZE)
    header[0:4] = b'DBPF'
    struct.pack_into('<II', header, 4, 2, 1)        # major.minor = 2.1
    struct.pack_into('<I', header, 36, len(resources))  # entry count
    struct.pack_into('<I', header, 44, index_size)   # index size
    struct.pack_into('<I', header, 60, 3)            # index minor version = 3
    struct.pack_into('<I', header, 64, index_offset) # index offset V2

    with open(out_path, 'wb') as f:
        f.write(bytes(header))
        f.write(bytes(body_blob))
        f.write(bytes(idx))


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

# No STBL is shipped with this mod -- our tunings reference existing
# base-game string hashes ("Call Someone", "Send Text", "Settings") so
# the player's installed Sims 4 already has the display names loaded.
#
# Rationale: authoring a STBL turned out to be unreliable. The on-disk
# format used by shipping mods has a 15-byte metadata block in the
# header (between the count field and the first entry) whose meaning we
# couldn't crack -- with the metadata zeroed out, the game's package
# loader refused to start at all. Borrowing base-game hashes sidesteps
# the STBL problem entirely.
STRINGS = {}

# STBL packaging:
#   group    = 0x80000000 (convention used by every shipping mod we've
#              inspected -- WorldTour, Basemental). At group=0 the game
#              silently ignores the STBL and tunings that reference it
#              render with blank display names.
#   instance = high byte is the language code (0x00 = English_US),
#              remaining 7 bytes are an arbitrary unique value.
STBL_GROUP = 0x80000000
STBL_INSTANCE = 0x0080000000000001


def main():
    repo = Path(__file__).resolve().parent.parent
    package_src = repo / "package_src"
    out_path = repo / "Llamafone.package"

    if not package_src.is_dir():
        print(f"ERROR: {package_src} not found")
        sys.exit(1)

    xml_files = sorted(package_src.glob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files in {package_src}")
        sys.exit(1)

    print(f"Building {out_path.name}...")

    resources = []

    # Only ship an STBL if we actually have strings to add. We don't --
    # see the STRINGS comment for why.
    if STRINGS:
        stbl_body = build_stbl_v5(STRINGS)
        resources.append((TYPE_STBL, STBL_GROUP, STBL_INSTANCE, stbl_body))
        print(f"  + STBL ({len(STRINGS)} strings)")

    # Tuning XMLs -- pick the resource type from the root <I i="..."> attribute
    for xml_path in xml_files:
        xml_text = xml_path.read_text(encoding='utf-8')
        instance, kind, tuning_name = read_tuning_attrs(xml_text)
        type_id = XML_TYPE_BY_KIND.get(kind, TYPE_TUNING)
        body = xml_text.encode('utf-8')
        resources.append((type_id, 0, instance, body))
        print(f"  + {kind:<20} {xml_path.name} (instance={hex(instance)}, type={hex(type_id)})")

        # PieMenuCategory tunings need a SimData companion under the same
        # instance ID so the client-side UI can render their tile. The
        # SimData goes under a SPECIFIC group (0xE9D967) -- a Sims 4
        # convention shared by every mod we've inspected (Basemental,
        # WorldTour). At group=0 the UI ignores it.
        if kind == "pie_menu_category":
            display_name = read_display_name_hash(xml_text) or 0
            simdata_body = build_pie_menu_category_simdata(display_name, tuning_name)
            resources.append((TYPE_SIMDATA, SIMDATA_PMC_GROUP, instance, simdata_body))
            print(f"  + SimData              {xml_path.stem} "
                  f"(instance={hex(instance)}, type={hex(TYPE_SIMDATA)}, "
                  f"group={hex(SIMDATA_PMC_GROUP)}, "
                  f"display_name=0x{display_name:08X})")

    build_package(resources, out_path)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"\nWrote: {out_path} ({size_kb:.1f} KB, {len(resources)} resources)")


if __name__ == "__main__":
    main()
