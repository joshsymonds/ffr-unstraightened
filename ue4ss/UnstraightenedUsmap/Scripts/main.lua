-- Dumps the game's mappings with DumpUSMAP once the game is up.
-- Writes "failed" when the dump raised, then a final "done", to usmap.log. Nothing else.
-- UE4SS writes the .usmap into ue4ss/; ffrd usmap fetches it from there.

local dir = debug.getinfo(1, "S").source:match("^@(.*)[/\\]Scripts[/\\]main%.lua$")
local out = io.open(dir .. "/usmap.log", "w")
if not out then return end

local function log(s) out:write(s, "\n"); out:flush() end

ExecuteWithDelay(20000, function()
    ExecuteInGameThread(function()
        if not pcall(DumpUSMAP, true) then log("failed") end
        log("done")
        out:close()
    end)
end)
