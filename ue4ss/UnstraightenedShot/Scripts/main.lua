-- Brings one dialogue line on screen for ffrd shot and holds it there while the host captures the window.
-- Inputs in this folder: slot.txt holds the dedicated save slot. title.txt holds the title tag's hash, or target.txt
-- holds the line's hash on its first line and then one route per line, tried in order until the line is on screen:
--   startup <map> <progress>            the map plays the scene on load at that story progress
--   npc <map> <x> <y> <z> <settings>     talk to the NPC placed there
--   trigger <map> <x> <y> <z> <settings> stand in the trigger placed there
--   event <id>                           play the event where the party stands
-- settings are counter=value pairs joined by ";" (or "-") written to the save's story progress before travel.
-- Writes only hashes, ids and states to shot.log, never the text: "snap <hash>" when the line is on screen, "held
-- <hash>" after the host writes go, and "done" last.

local K = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
}
local M = 0xffffffff
local function rotr(x, n) return ((x >> n) | (x << (32 - n))) & M end
local function sha256(msg)
    local len = #msg
    msg = msg .. "\128" .. string.rep("\0", (55 - len) % 64) .. string.pack(">I8", len * 8)
    local H = { 0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19 }
    local w = {}
    for chunk = 1, #msg, 64 do
        for i = 0, 15 do w[i] = string.unpack(">I4", msg, chunk + i * 4) end
        for i = 16, 63 do
            local a, b = w[i - 15], w[i - 2]
            local s0 = rotr(a, 7) ~ rotr(a, 18) ~ (a >> 3)
            local s1 = rotr(b, 17) ~ rotr(b, 19) ~ (b >> 10)
            w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & M
        end
        local a, b, c, d, e, f, g, h = H[1], H[2], H[3], H[4], H[5], H[6], H[7], H[8]
        for i = 0, 63 do
            local t1 = (h + (rotr(e, 6) ~ rotr(e, 11) ~ rotr(e, 25)) + ((e & f) ~ (~e & g)) + K[i + 1] + w[i]) & M
            local t2 = ((rotr(a, 2) ~ rotr(a, 13) ~ rotr(a, 22)) + ((a & b) ~ (a & c) ~ (b & c))) & M
            h, g, f, e, d, c, b, a = g, f, e, (d + t1) & M, c, b, a, (t1 + t2) & M
        end
        H[1], H[2], H[3], H[4] = (H[1] + a) & M, (H[2] + b) & M, (H[3] + c) & M, (H[4] + d) & M
        H[5], H[6], H[7], H[8] = (H[5] + e) & M, (H[6] + f) & M, (H[7] + g) & M, (H[8] + h) & M
    end
    return string.format(string.rep("%08x", 8), table.unpack(H))
end

local dir = debug.getinfo(1, "S").source:match("^@(.*)[/\\]Scripts[/\\]main%.lua$")
local out = io.open(dir .. "/shot.log", "w")
if not out then return end
local function log(s) out:write(s, "\n"); out:flush() end
local function read(name)
    local f = io.open(dir .. "/" .. name, "r")
    if not f then return nil end
    local s = f:read("a"); f:close()
    return s
end
local function valid(o) local ok, v = pcall(function() return o ~= nil and o:IsValid() end); return ok and v end

local SLOT = tonumber(read("slot.txt") or "")
local TITLE = (read("title.txt") or ""):match("%x+")
local TARGET, ROUTES = nil, {}
for line in (read("target.txt") or ""):gmatch("[^\n]+") do
    if not TARGET then
        TARGET = line:match("^%x+")
    else
        local words = {}
        for w in line:gmatch("%S+") do words[#words + 1] = w end
        local route = { kind = words[1] }
        if route.kind == "startup" then
            route.map, route.x, route.y, route.z = tonumber(words[2]), 0.0, 0.0, 0.0
            route.settings = { { 0, tonumber(words[3]) } }
        elseif route.kind == "npc" or route.kind == "trigger" then
            route.map, route.x, route.y, route.z = tonumber(words[2]), tonumber(words[3]), tonumber(words[4]), tonumber(words[5])
            route.settings = {}
            for n, v in (words[6] or ""):gmatch("(%d+)=(%d+)") do route.settings[#route.settings + 1] = { tonumber(n), tonumber(v) } end
        elseif route.kind == "event" then
            route.id = words[2]
        end
        ROUTES[#ROUTES + 1] = route
    end
end

local TALK = nil
local function playing() local ok, v = pcall(function() return TALK:IsPlayingEvent() end); return ok and v end

-- The line in the talk window, hashed, and the window to advance it.
local function talk()
    local sub = FindFirstOf("CPP_TalkEventSubsystem")
    if not valid(sub) then return nil end
    local mgr = sub.m_pEventManager
    if not valid(mgr) then return nil end
    local w = mgr.m_pWidget
    if not valid(w) then return nil end
    local td = w.m_pCurrentTalkData
    if not valid(td) then return nil, w end
    local ok, s = pcall(function() return td:GetText():ToString() end)
    if not ok then return nil, w end
    return sha256(s):sub(1, 16), w
end

local function title_hash()
    local ktl = StaticFindObject("/Script/Engine.Default__KismetTextLibrary")
    local o = {}
    if ktl:FindTextInLocalizationTable("ST_UI_TitleMenu", "UI_Title_Copyright", o, "") then
        return sha256(o.OutText:ToString()):sub(1, 16)
    end
    return "missing"
end

-- Writes the route's story progress and destination into the dedicated slot and loads it, the way the game's own
-- map start reads a slot. An NPC route arrives beside the NPC; the others arrive at the point itself.
local function travel(route)
    local sd = FindFirstOf("CPP_MuchaSaveDataManager").m_pGameSaveData
    for _, nv in ipairs(route.settings) do sd.Progress.Progress[nv[1] + 1] = nv[2] end
    sd.Map.mapId = route.map
    sd.Map.unitLocation.X = route.x + (route.kind == "npc" and 120.0 or 0.0)
    sd.Map.unitLocation.Y = route.y
    sd.Map.unitLocation.Z = route.z
    StaticFindObject("/Script/mucha.Default__CPP_BFL_SaveData"):SaveGameData(SLOT)
    FindFirstOf("CPP_MapManager"):MapTestStartByChoiceSlotLoadSaveData(SLOT)
end

-- Faces the NPC and presses interact, as the player would.
local function interact(route)
    local player = FindFirstOf("CPP_MapUnit_Player")
    player:SetDirectionToLocation({ X = route.x, Y = route.y, Z = route.z })
    player:InputInteract({})
end

local state, since, tick, ri = "boot", 0, 0, 0
local last_h, last_change, last_proceed, started
local function go(s, now) state, since = s, now end
local function finish() state = "finished" end
local function route() return ROUTES[ri] end
local function begin_watch(now) last_h, last_change, last_proceed, started = nil, now, now, false end

-- Advances the dialogue a line a second, logs each new line's hash and stops on the target.
local function watch_line(now)
    local h, w = talk()
    if h and h ~= last_h then
        last_h, last_change, last_proceed = h, now, now
        log("line " .. h)
        if h == TARGET then log("snap " .. h); go("hold", now); return true end
    end
    if w and now - last_proceed >= 1 then pcall(function() w:HandleProceedButtonClicked() end); last_proceed = now end
    return false
end

LoopAsync(500, function()
    tick = tick + 1
    local now = tick * 0.5
    ExecuteInGameThread(function()
        local ok = pcall(function()
            if state == "boot" then
                if now >= 20 and valid(FindFirstOf("WBP_TitleLayout_C")) then go("title", now) end
            elseif state == "title" then
                if now - since < 8 then return end
                if TITLE then log("snap " .. title_hash()); go("hold_title", now); return end
                TALK = StaticFindObject("/Script/mucha.Default__CPP_BFL_TalkEvent")
                for _, vm in ipairs(FindAllOf("VM_Title_C") or {}) do
                    if not vm:GetFullName():find("Default__") and pcall(function() vm:ExecuteEntry(0) end) then break end
                end
                go("field", now)
            elseif state == "hold_title" then
                if read("go") then log("held " .. title_hash()); finish() end
                if now - since > 240 then log("hold timeout"); finish() end
            elseif state == "field" then
                local mm = FindFirstOf("CPP_MapManager")
                if valid(mm) and now - since > 25 and not playing() then
                    log("slotload " .. tostring(pcall(function() mm:MapTestStartByChoiceSlotLoadSaveData(SLOT) end)))
                    go("settle", now)
                elseif now - since > 120 then log("field timeout"); finish() end
            elseif state == "settle" then
                if now - since > 30 and valid(FindFirstOf("CPP_MapManager")) and not playing() then go("next_route", now) end
                if now - since > 150 then log("settle timeout"); finish() end
            elseif state == "next_route" then
                ri = ri + 1
                local r = route()
                if not r then log("miss"); finish(); return end
                begin_watch(now)
                if r.kind == "event" then
                    pcall(function() TALK:PlayEvent(FName(r.id, EFindName.FNAME_Add), 0) end)
                    log("route event " .. r.id)
                    go("event", now)
                else
                    log("route " .. r.kind .. " " .. r.map .. " " .. tostring(pcall(travel, r)))
                    go("arrive", now)
                end
            elseif state == "arrive" then
                -- A startup or trigger route starts its scene on its own; an NPC route waits for the map, then talks.
                local r = route()
                if r.kind == "npc" then
                    if now - since > 35 and valid(FindFirstOf("CPP_MapManager")) and not playing() then
                        log("interact " .. tostring(pcall(interact, r)))
                        begin_watch(now)
                        go("scene", now)
                    elseif now - since > 150 then log("arrive timeout"); go("next_route", now) end
                elseif playing() or talk() then
                    log("started")
                    begin_watch(now)
                    started = true
                    go("scene", now)
                elseif now - since > 120 then log("never started"); go("next_route", now) end
            elseif state == "scene" then
                if playing() then started = true end
                if watch_line(now) then return end
                if last_h == nil and now - since > 40 then log("no line"); go("next_route", now); return end
                if last_h ~= nil and now - last_change > 25 then go("next_route", now) end
            elseif state == "event" then
                if playing() then started = true end
                if watch_line(now) then return end
                if not started and now - since > 4 and not talk() then go("gap", now); return end
                if started and not playing() then go("gap", now); return end
                if now - last_change > 8 then pcall(function() TALK:StopEvent() end); go("gap", now) end
            elseif state == "gap" then
                if (now - since > 2 and not playing()) or now - since > 15 then go("next_route", now) end
            elseif state == "hold" then
                if read("go") then log("held " .. tostring(talk() or "none")); finish() end
                if now - since > 240 then log("hold timeout"); finish() end
            end
        end)
        if not ok then log("error " .. state); if state == "event" or state == "scene" then go("next_route", now) end end
    end)
    if state == "finished" or tick > 7200 then log("done"); out:close(); return true end
    return false
end)
