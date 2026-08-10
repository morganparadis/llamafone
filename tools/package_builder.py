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
# Sims 4 stores UI image resources at type 0x00B2D882. The tuning
# XML's `_icon` field references the resource via a type alias
# (2f7d0004 -> 0x00B2D882). World Tour ships images at group=0x80000000
# but their SimData references them at group=0x00000000 -- some kind
# of aliasing. Since the aliasing might not work reliably for third-
# party mods, shipping the actual resource at the group our SimData
# points to (0x00000000) is the direct route.
TYPE_IMAGE = 0x00B2D882
IMAGE_GROUP = 0x00000000

# Instance ID used for our own bundled phone-app icon. Deterministic
# (FNV-64 of the filename) so the SimData / PieMenuCategory / interaction
# XMLs can hard-reference the same instance without a build step lookup.
# Any 64-bit unique value would work; using a hash keeps it reproducible
# and out of any obvious collision range.
def fnv1_64_lower(name):
    h = 0xCBF29CE484222325
    for c in name.lower().encode('ascii'):
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        h ^= c
    return h

LLAMAFONE_ICON_INSTANCE = fnv1_64_lower("llamafone_icon")  # 0x27... (stable)

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
    # Extracted from thatssojordy_WorldTour_CORE_V2.1 -- the SimData for
    # tuning `thatssojordy_WorldTour_PieMenu`. This is a top-level phone-
    # home category (no _parent), display_name is a real STBL hash, and
    # every field required by the CURRENT game schema is present including
    # `always_show_disabled_interactions` which Basemental's older
    # template lacked. Basemental's 425-byte template loaded without a
    # crash but the game silently skipped the icon field lookup because
    # the schema didn't match what the current client expects.
    "4441544101010000180000000100000070000000010000000000000000000000"
    "a601000050a1ea08580000000d000000400000000c0000000100000000000000"
    "0000000077893fa0010000000000000043ee06a0e365f69382d8b20000000000"
    "0000000000000000000000000000000001000000000000800000000000000000"
    "36010000ccad21db41a1eae2400000000800000008000000d3000000806d6139"
    "120000002000000000000080c70000002c57a95b080000002800000000000080"
    "78000000a85f00920000000000000000000000807f000000296a5da406000000"
    "08000000000000807d0000001b7ef1ae13000000100000000000008049000000"
    "b8be3ad2140000000400000000000080970000009e304bda0e00000034000000"
    "00000080610000007f07c2e00000000030000000000000805f636f6c6c617073"
    "69626c65005f646973706c61795f6e616d65005f646973706c61795f7072696f"
    "72697479005f69636f6e005f706172656e74005f7370656369616c5f63617465"
    "676f727900616c776179735f73686f775f64697361626c65645f696e74657261"
    "6374696f6e73006d6f6f645f6f7665727269646573005069654d656e75436174"
    "65676f7279007468617473736f6a6f7264795f576f726c64546f75725f506965"
    "4d656e7500"
)
# Patch points within the World Tour-derived template. The schema
# gained `always_show_disabled_interactions` between Basemental's
# capture and World Tour's, which shifted the display_name field to
# 0x44 and pushed the trailing instance-name string to 0x1C6.
#   0x24-0x27  TableInfo.NameHash -- FNV-1 32-bit of lowercased tuning name
#   0x44-0x47  display_name STBL hash (u32 LE)
#   0x50-0x57  _icon instance (u64 LE)
#   0x58-0x5B  _icon type (u32 LE)
#   0x5C-0x5F  _icon group (u32 LE)
#   0x1C6+     null-terminated tuning instance name string
#
# display_priority isn't patched anymore: the World Tour template
# ships with priority=1 which is fine for a phone-home tile. If we
# ever need to override it we'll need to find the shifted offset.
_SD_NAME_HASH_OFFSET = 0x24
_SD_DISPLAY_NAME_OFFSET = 0x44
_SD_ICON_INSTANCE_OFFSET = 0x50
_SD_ICON_TYPE_OFFSET = 0x58
_SD_ICON_GROUP_OFFSET = 0x5C
_SD_NAME_STRING_OFFSET = 0x1C6
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
    display_name_hash, instance_name,
    icon_type=_SD_ICON_TYPE,
    icon_group=_DEFAULT_ICON_GROUP,
    icon_instance=_DEFAULT_ICON_INSTANCE,
):
    """Patch the captured template with this category's display_name hash,
    icon ResourceKey, and trailing instance-name string (plus the
    recomputed NameHash that maps to it). Returns a 485-byte SimData
    blob ready to pack.

    display_priority is intentionally left as the template's baked-in
    value (1) -- the current schema shifted its offset and I don't
    have a solid mapping yet. All the phone-home tiles we care about
    look fine at priority=1."""
    data = bytearray(PIE_MENU_CATEGORY_SIMDATA_TEMPLATE)
    struct.pack_into('<I', data, _SD_NAME_HASH_OFFSET, fnv1_32_lower(instance_name))
    struct.pack_into('<I', data, _SD_DISPLAY_NAME_OFFSET, display_name_hash)
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

    Format reverse-engineered from World Tour's shipping STBLs. All 18
    of thatssojordy_WorldTour_CORE's STBLs and all 18 of the album
    STBLs walk cleanly with this layout (walk consumes exactly N bytes
    for an N-byte resource).

    Header (21 bytes):
      4  MAGIC 'STBL'
      1  version         = 0x05
      2  compressed      (u16 LE) = 0
      8  numEntries      (u64 LE)
      2  reserved        (u16 LE) = 0
      4  mnStringLength  (u32 LE) = sum(len(s)) + numEntries
                                    (World Tour counts one extra byte
                                    per entry -- probably a virtual nul
                                    terminator that isn't on disk)

    Entry (7-byte header + N bytes string, repeated numEntries times):
      4  key     (u32 LE)
      1  flags   (u8)     = 0
      2  length  (u16 LE)
      N  string bytes UTF-8 (no null terminator on disk)

    Prior attempts used a 22-byte header (extra padding u32) and no
    per-entry flags byte, which parses "well enough" that the game
    doesn't reject the .package outright but silently fails hash
    lookup so display_names never resolve. This layout matches World
    Tour byte-for-byte for the same input -- see
    tools/analyze_stbl in scratchpad.
    """
    encoded = [(k, s.encode('utf-8')) for k, s in strings.items()]
    total_str_bytes = sum(len(b) for _, b in encoded)
    num_entries = len(encoded)

    out = bytearray()
    out += b'STBL'
    out += bytes([0x05])                                    # version
    out += struct.pack('<H', 0)                             # compressed
    out += struct.pack('<Q', num_entries)                   # numEntries
    out += struct.pack('<H', 0)                             # reserved
    out += struct.pack('<I', total_str_bytes + num_entries) # mnStringLength
    for key, raw in encoded:
        out += struct.pack('<I', key)                       # key
        out += bytes([0])                                   # flags
        out += struct.pack('<H', len(raw))                  # length
        out += raw                                          # UTF-8 bytes
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


def read_icon_key(xml_text):
    """Pull the `_icon` resource key from a tuning XML. Returns
    (type, group, instance) as ints, or (None, None, None) when the
    field is missing or unparseable.

    Two accepted XML shapes:

    Flat (World Tour / MC Command Center / most modern mods):
        <T n="_icon" p="path/for/humans">2f7d0004:00000000:XXXXXXX</T>

    Nested (what Sims4Studio autogenerates from GUI edits):
        <V n="_icon" t="resource_key">
          <U n="resource_key">
            <T n="key" p="...">2f7d0004:00000000:XXXXXXX</T>
          </U>
        </V>

    The client-side ActionScript that renders phone tiles only
    recognises the flat form for `_icon` on a PieMenuCategory --
    the nested form parses server-side but produces a broken tile
    (verified empirically). Interactions (tuning kind='interaction')
    happily accept either form for their own `_icon` field.
    """
    # Try flat form first (single <T n="_icon"> with the resource-key
    # text as its body).
    m = re.search(
        r'<T\s+n="_icon"[^>]*>([0-9a-fA-F:]+)</T>',
        xml_text,
    )
    if not m:
        # Try nested form.
        m = re.search(
            r'<V\s+n="_icon"\s+t="resource_key"\s*>\s*'
            r'<U\s+n="resource_key"\s*>\s*'
            r'<T\s+n="key"[^>]*>([^<]+)</T>',
            xml_text,
        )
    if not m:
        return (None, None, None)
    parts = m.group(1).strip().split(':')
    if len(parts) != 3:
        return (None, None, None)
    try:
        return (int(parts[0], 16), int(parts[1], 16), int(parts[2], 16))
    except ValueError:
        return (None, None, None)


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

# STBL strings shipped by this mod. Keys are the u32 hashes that
# tunings reference via <T n="_display_name">HASH</T>. Values are the
# UTF-8 text the game renders. Adding a string here + referencing its
# hash from a tuning is all that's needed -- build_stbl_v5() takes it
# from there.
#
# 0x00CA1E00 -> "Llamafone": hover text for the top-level phone tile,
# referenced by package_src/phoneCategory_Llamafone.xml. Top byte of
# the hash is 0 (matches the FNV-24 range shipping STBLs use for
# authored hashes) but the value is arbitrary -- what matters is that
# it matches the tuning reference exactly.
STRINGS = {
    0x00CA1E00: "Llamafone",
    0x00CA1F00: "Llamadate",
}

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

    # DDS image assets. Any .dds in package_src/ gets embedded as an
    # IMAGE resource. Type/group are Sims 4's UI-image conventions
    # (see TYPE_IMAGE / IMAGE_GROUP above); instance ID is
    # deterministic (FNV-64 of the bare filename) so tuning XMLs can
    # hard-reference it without a lookup pass.
    #
    # DDS specifically (not PNG): the game's image decoder at type
    # 0x00B2D882 expects DDS magic bytes. Feeding it a PNG resource
    # decodes to "?" and the phone tile renders with the fallback
    # placeholder. Every shipping mod that gets icons working uses
    # DDS. Convert source PNGs via Pillow's Image.save(fmt='DDS')
    # before dropping them in package_src/.
    for dds_path in sorted(package_src.glob("*.dds")):
        stem = dds_path.stem  # e.g. "llamafone_icon"
        instance = fnv1_64_lower(stem)
        body = dds_path.read_bytes()
        resources.append((TYPE_IMAGE, IMAGE_GROUP, instance, body))
        print(f"  + DDS                 {dds_path.name} "
              f"(instance={hex(instance)}, type={hex(TYPE_IMAGE)}, "
              f"group={hex(IMAGE_GROUP)}, {len(body):,} bytes)")

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
            # Pull the icon resource key from the XML so the SimData
            # references the SAME icon the tuning does. Without this,
            # the SimData would default to the base-game cellphone icon
            # and the phone home would show our tuning's category with
            # a plain phone icon instead of our custom PNG.
            icon_type, icon_group, icon_inst = read_icon_key(xml_text)
            simdata_body = build_pie_menu_category_simdata(
                display_name, tuning_name,
                icon_group=icon_group if icon_type else _DEFAULT_ICON_GROUP,
                icon_instance=icon_inst if icon_type else _DEFAULT_ICON_INSTANCE,
            )
            resources.append((TYPE_SIMDATA, SIMDATA_PMC_GROUP, instance, simdata_body))
            print(f"  + SimData              {xml_path.stem} "
                  f"(instance={hex(instance)}, type={hex(TYPE_SIMDATA)}, "
                  f"group={hex(SIMDATA_PMC_GROUP)}, "
                  f"display_name=0x{display_name:08X}, "
                  f"icon_instance={hex(icon_inst) if icon_inst else '(default)'})")

    build_package(resources, out_path)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"\nWrote: {out_path} ({size_kb:.1f} KB, {len(resources)} resources)")


if __name__ == "__main__":
    main()
