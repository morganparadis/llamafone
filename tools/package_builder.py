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


def build_stbl_v5(strings):
    """Build a v5 STBL resource body from {key_int: utf8_string}.

    Layout:
      'STBL' (4)
      version (1)        = 0x05
      compressed (1)     = 0x00 (the STBL itself isn't internally compressed)
      count (u64)
      mn_string_length (u32) = 0 (legacy field, ignored)
      total_str_len (u32)    = sum of string byte-lengths
      then for each entry:
        key (u32) + flags (u8) + length (u16) + utf8 bytes
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
        out += bytes([0])
        out += struct.pack('<H', len(raw))
        out += raw
    return bytes(out)


def read_tuning_attrs(xml_text):
    """Extract the root <I ...> attributes from a tuning XML.

    Returns (instance_id_int, i_kind_string). i_kind tells us which
    resource type ID to use (e.g. 'interaction' vs 'pie_menu_category').
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
    return instance, kind


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

# Strings used by the pie-menu interactions. Keys are referenced from
# the XMLs as 0x00000001, 0x00000002 etc.
STRINGS = {
    0x00000001: "Claude AI",       # PieMenuCategory display name (the wedge)
    0x00000002: "Claude AI Menu",  # Interaction display name (item inside the wedge)
}

# Pick an STBL instance: high byte = language (0x00 = English),
# remaining bytes are arbitrary. Standard convention is
# 0x00FF...something or just a unique value.
STBL_INSTANCE = 0x0080000000000001


def main():
    repo = Path(__file__).resolve().parent.parent
    package_src = repo / "package_src"
    out_path = repo / "ClaudeAI.package"

    if not package_src.is_dir():
        print(f"ERROR: {package_src} not found")
        sys.exit(1)

    xml_files = sorted(package_src.glob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files in {package_src}")
        sys.exit(1)

    print(f"Building {out_path.name}...")

    resources = []

    # STBL first
    stbl_body = build_stbl_v5(STRINGS)
    resources.append((TYPE_STBL, 0, STBL_INSTANCE, stbl_body))
    print(f"  + STBL ({len(STRINGS)} strings)")

    # Tuning XMLs -- pick the resource type from the root <I i="..."> attribute
    for xml_path in xml_files:
        xml_text = xml_path.read_text(encoding='utf-8')
        instance, kind = read_tuning_attrs(xml_text)
        type_id = XML_TYPE_BY_KIND.get(kind, TYPE_TUNING)
        body = xml_text.encode('utf-8')
        resources.append((type_id, 0, instance, body))
        print(f"  + {kind:<20} {xml_path.name} (instance={hex(instance)}, type={hex(type_id)})")

    build_package(resources, out_path)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"\nWrote: {out_path} ({size_kb:.1f} KB, {len(resources)} resources)")


if __name__ == "__main__":
    main()
