-- Logs a hash of each localization key listed in keys.txt, once per change.
-- Writes only "<namespace>,<key> <hash>", "missing" and "done" to probe.log.
-- Never writes the text itself.

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
local out = io.open(dir .. "/probe.log", "w")
if not out then return end
local keyfile = io.open(dir .. "/keys.txt", "r")
if not keyfile then
    out:close()
    return
end
local keys = {}
for line in keyfile:lines() do
    local ns, key = line:match("^([^,]+),(.+)$")
    if ns then keys[#keys + 1] = { ns, key } end
end
keyfile:close()

local function log(s) out:write(s, "\n"); out:flush() end
local last, ticks = {}, 0
local function lookup()
    local ktl = StaticFindObject("/Script/Engine.Default__KismetTextLibrary")
    if not ktl:IsValid() then return end
    for _, k in ipairs(keys) do
        local o = {}
        local found = ktl:FindTextInLocalizationTable(k[1], k[2], o, "")
        local h = found and sha256(o.OutText:ToString()):sub(1, 16) or "missing"
        local id = k[1] .. "," .. k[2]
        if last[id] ~= h then last[id] = h; log(id .. " " .. h) end
    end
end
LoopAsync(1000, function()
    ticks = ticks + 1
    if ticks >= 30 then
        log("done")
        out:close()
        return true
    end
    ExecuteInGameThread(lookup)
    return false
end)
