# map.md hybrid protocol — SHA-staleness check, cheap path first
When you need to understand a source file:
1. Check if a `.map.md` file exists next to it (e.g. `foo.py` → `foo.py.map.md`)
2. If it exists, call `check_stale("foo.py")` to verify it's fresh via SHA-256
3. If `check_stale` returns `{"stale": false}`, read the `.map.md` file directly from disk — cheap path
4. If no `.map.md` exists OR `check_stale` returns `{"stale": true}`, call `map_file("foo.py")` to generate a fresh map
5. After getting the map (disk or MCP), use `read_source` to read only the specific line ranges you need
6. Do not read entire source files unless a required line range is unclear from the map
