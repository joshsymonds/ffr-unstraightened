-- Registers each line of edits.txt as en polyglot text, once the game's text libraries are valid.
-- Registers only when the current culture is en or en-*; in any other culture the game's own text stays.
-- edits.txt lines: namespace<TAB>key<TAB>text<LF>, with backslash, tab, CR and LF escaped in each field.
-- Never writes a file and never prints text.

local TEXT_LIBRARY = "/Script/Engine.Default__KismetTextLibrary"
local CULTURE_LIBRARY = "/Script/Engine.Default__KismetInternationalizationLibrary"
local UNESCAPE = { n = "\n", r = "\r", t = "\t", ["\\"] = "\\" }

local function unescape(s) return (s:gsub("\\(.)", UNESCAPE)) end

local dir = debug.getinfo(1, "S").source:match("^@(.*)[/\\]Scripts[/\\]main%.lua$")
local edits = {}
local f = io.open(dir .. "/edits.txt", "r")
if not f then return end
for line in f:lines() do
    local ns, key, text = line:match("^([^\t]+)\t([^\t]+)\t(.*)$")
    if ns then edits[#edits + 1] = { unescape(ns), unescape(key), unescape(text) } end
end
f:close()

local function register()
    local ktl = StaticFindObject(TEXT_LIBRARY)
    local kil = StaticFindObject(CULTURE_LIBRARY)
    if not (ktl:IsValid() and kil:IsValid()) then return false end
    local culture = kil:GetCurrentCulture():ToString()
    if culture == "en" or culture:match("^en%-") then
        for _, e in ipairs(edits) do
            ktl:PolyglotDataToText({
                Category = 0,
                NativeCulture = "en",
                Namespace = e[1],
                Key = e[2],
                NativeString = e[3],
                bIsMinimalPatch = false,
                CachedText = FText(""),
            })
        end
    end
    return true
end

local finished = false
LoopAsync(1000, function()
    if finished then return true end
    ExecuteInGameThread(function()
        if not finished then finished = register() end
    end)
    return false
end)
