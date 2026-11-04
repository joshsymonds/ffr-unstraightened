-- Brings one dialogue line on screen for ffrd shot and holds it there while the host captures the window.
-- Inputs in this folder: target.txt holds "<hash>\t<event,event,...>\t<map:progress;map:progress>", or title.txt holds
-- the title tag's hash. Writes only hashes, ids and states to shot.log, never the text: "snap <hash>" when the line is
-- on screen, "held <hash>" after the host writes go, and "done" last.

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
local TITLE = read("title.txt")
local TARGET, EVENTS, STARTUPS = nil, {}, {}
local spec = read("target.txt")
if spec then
    local h, evs, ups = spec:match("^(%x+)\t([^\t]*)\t([^\t\n]*)")
    TARGET = h
    for e in (evs or ""):gmatch("[^,]+") do EVENTS[#EVENTS + 1] = e end
    for m, p in (ups or ""):gmatch("(%d+):(%d+)") do STARTUPS[#STARTUPS + 1] = { map = tonumber(m), progress = tonumber(p) } end
end
if TITLE then TITLE = TITLE:match("%x+") end

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

-- Puts the save on a map at a story progress in the dedicated slot and loads it, the way the map's own startup
-- events expect: the map plays the event on load when the progress matches.
local function travel(up)
    local sd = FindFirstOf("CPP_MuchaSaveDataManager").m_pGameSaveData
    sd.Progress.Progress[1] = up.progress
    sd.Map.mapId = up.map
    sd.Map.unitLocation.X = 0.0
    sd.Map.unitLocation.Y = 0.0
    sd.Map.unitLocation.Z = 0.0
    StaticFindObject("/Script/mucha.Default__CPP_BFL_SaveData"):SaveGameData(SLOT)
    FindFirstOf("CPP_MapManager"):MapTestStartByChoiceSlotLoadSaveData(SLOT)
end

local state, since, tick = "boot", 0, 0
local ui, ei = 0, 0
local last_h, last_change, last_proceed, started
local function go(s, now) state, since = s, now end
local function finish() state = "finished" end

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
        local ok, err = pcall(function()
            if state == "boot" then
                if now >= 20 and valid(FindFirstOf("WBP_TitleLayout_C")) then go("title", now) end
            elseif state == "title" then
                if now - since < 8 then return end
                if TITLE then
                    local h = title_hash()
                    log("snap " .. h)
                    go("hold_title", now)
                    return
                end
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
                if now - since > 30 and valid(FindFirstOf("CPP_MapManager")) and not playing() then
                    go(#STARTUPS > 0 and "next_startup" or "next_event", now)
                end
                if now - since > 150 then log("settle timeout"); finish() end
            elseif state == "next_startup" then
                ui = ui + 1
                if ui > #STARTUPS then log("miss"); finish(); return end
                local up = STARTUPS[ui]
                log("travel " .. up.map .. " " .. up.progress .. " " .. tostring(pcall(travel, up)))
                last_h, started = nil, false
                go("startup", now)
            elseif state == "startup" then
                if not started and (playing() or talk()) then started = true; last_change = now; log("startup " .. STARTUPS[ui].map) end
                if started then
                    if watch_line(now) then return end
                    if (not playing() and last_h ~= nil) or now - last_change > 25 then go("next_startup", now) end
                elseif now - since > 120 then log("startup never played " .. STARTUPS[ui].map); go("next_startup", now) end
            elseif state == "next_event" then
                ei = ei + 1
                if ei > #EVENTS then log("miss"); finish(); return end
                local ev = EVENTS[ei]
                pcall(function() TALK:PlayEvent(FName(ev, EFindName.FNAME_Add), 0) end)
                last_h, last_change, last_proceed, started = nil, now, now, false
                go("event", now)
            elseif state == "event" then
                if playing() then started = true end
                if watch_line(now) then return end
                if not started and now - since > 4 and not talk() then log("noplay " .. EVENTS[ei]); go("gap", now); return end
                if started and not playing() then go("gap", now); return end
                if now - last_change > 8 then pcall(function() TALK:StopEvent() end); go("gap", now) end
            elseif state == "gap" then
                if (now - since > 2 and not playing()) or now - since > 15 then go("next_event", now) end
            elseif state == "hold" then
                if read("go") then log("held " .. tostring(talk() or "none")); finish() end
                if now - since > 240 then log("hold timeout"); finish() end
            end
        end)
        if not ok then log("error " .. state); if state == "event" then go("gap", now) end end
    end)
    if state == "finished" or tick > 4800 then log("done"); out:close(); return true end
    return false
end)
