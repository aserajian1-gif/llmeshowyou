# map.md hybrid protocol — cheap path first, fresh always
When you need to understand a source file:
1. Check if a `.map.md` file exists next to it (e.g. `foo.py.map.md`)
2. If it exists and is NOT stale (source modified time <= map modified time), read the map file directly — cheap, no MCP call needed
3. If no `.map.md` exists OR the source is newer than the map (stale), call the `map_file` MCP tool instead to generate a fresh map
4. After getting the map (from disk or MCP), use `read_source` MCP tool to read only the specific line ranges you need from the source file
5. Do not read entire source files unless a required line range is unclear from the map
