# map.md priority (ephemeral, via MCP tool)
When you need to understand a source file, call the `map_file` MCP tool first before reading source files directly. The tool generates a fresh structural map showing classes, functions, signatures, line ranges, imports, constants, section markers, and TODOs — always from current source on disk, never stale.

After getting the map, use `read_source` to read only the specific line ranges you need from the source file. Do not read entire files unless a required line range is unclear from the map.

If `map_file` is unavailable, fall back to reading `.map.md` files from disk, then source files as a last resort.
