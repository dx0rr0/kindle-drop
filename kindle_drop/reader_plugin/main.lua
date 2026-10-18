-- Kindle Drop managed auto-refresh plugin v1
-- v2: also files finished articles under Leídos/<month> and runs moves the PC queued.
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local UIManager = require("ui/uimanager")
local DataStorage = require("datastorage")
local DocSettings = require("docsettings")
local ReadCollection = require("readcollection")
local ReadHistory = require("readhistory")
local lfs = require("libs/libkoreader-lfs")
local util = require("util")
local logger = require("logger")

local READ_FOLDER = "Leídos"
local MONTHS = { "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
                 "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre" }
local ARCHIVE_TICKS = 12 -- about a minute between looks at the articles folder
local MAX_MOVES = 200

local KindleDrop = WidgetContainer:extend{
    name = "kindle_drop_refresh",
    is_doc_only = false,
}

function KindleDrop:init()
    -- ReaderUI also instantiates plugins. Do no polling while reading a book.
    if self.ui.document then return end
    self.enabled = not G_reader_settings:isFalse("kindle_drop_auto_refresh")
    self.archiving = not G_reader_settings:isFalse("kindle_drop_archive")
    self.marker = DataStorage:getSettingsDir() .. "/kindle-drop.refresh"
    self.moves = DataStorage:getSettingsDir() .. "/kindle-drop.moves"
    self.ui.menu:registerToMainMenu(self)
    -- The browser is rebuilt each time a book closes: look first, so a finished article is filed at once.
    self.ticks = ARCHIVE_TICKS
    self.tick = function()
        if self.closed or self.suspended or not self.enabled then return end
        local ok, err = pcall(self.checkDelivery, self)
        if not ok then logger.warn("Kindle Drop refresh:", err) end
        UIManager:scheduleIn(5, self.tick)
    end
    if self.enabled then UIManager:scheduleIn(1, self.tick) end
end

local function inside(path, folder)
    return path:sub(1, #folder + 1) == folder .. "/"
end

local function readSmall(path, limit)
    local file = io.open(path, "rb")
    if not file then return end
    local content = file:read(limit + 1)
    file:close()
    if content and #content <= limit then return content end
end

function KindleDrop:checkDelivery()
    -- Defer while a menu/dialog covers the browser; never change folders or sorting.
    if self.ui.tearing_down or UIManager:getTopmostVisibleWidget() ~= self.ui then return end
    local chooser = self.ui.file_chooser
    if not chooser then return end
    local here = chooser.path:gsub("/+$", "")
    local changed
    local content = readSmall(self.marker, 4096)
    local nonce, folder, articles
    if content then
        nonce, folder, articles = content:match("^(%x+)\n([^\n]+)\n([^\n]+)\n$")
        if not nonce then nonce, folder = content:match("^(%x+)\n([^\n]+)\n$") end
    end
    if articles and articles:sub(1, 1) == "/" and not articles:find("/%.%.") and
            articles ~= G_reader_settings:readSetting("kindle_drop_articles") then
        G_reader_settings:saveSetting("kindle_drop_articles", articles)
    end
    if nonce and #nonce == 32 and nonce ~= self.seen then
        folder = folder:gsub("/+$", "")
        -- The delivery folder itself, or its parent showing the folder's item count.
        if here == folder or inside(folder, here) then
            changed = true
            self.seen = nonce
        end
    end
    self.ticks = self.ticks + 1
    if self.archiving and self.ticks >= ARCHIVE_TICKS then
        self.ticks = 0
        local dir = G_reader_settings:readSetting("kindle_drop_articles")
        if dir and (self:runMoves(dir) + self:archive(dir)) > 0 and (here == dir or inside(here, dir) or inside(dir, here)) then
            changed = true
        end
    end
    if changed then
        chooser:clearSortingCache()
        self.ui:onRefresh()
        logger.info("Kindle Drop: refreshed the file browser")
    end
end

-- The same steps as cutting and pasting in the file browser: the book, its .sdr, History and collections.
function KindleDrop:moveDocument(from, to)
    if lfs.attributes(from, "mode") ~= "file" or lfs.attributes(to, "mode") then return false end
    local folder = to:match("^(.*)/[^/]+$")
    util.makePath(folder)
    if lfs.attributes(folder, "mode") ~= "directory" or not self.ui:moveFile(from, folder) then return false end
    if lfs.attributes(to, "mode") ~= "file" then return false end
    DocSettings.updateLocation(from, to)
    ReadHistory:updateItem(from, to)
    ReadCollection:updateItem(from, to)
    logger.info("Kindle Drop: moved", from, "to", to)
    return true
end

function KindleDrop:archive(dir)
    if lfs.attributes(dir, "mode") ~= "directory" then return 0 end
    local finished = {}
    for name in lfs.dir(dir) do
        local path = dir .. "/" .. name
        if name:sub(1, 1) ~= "." and name:lower():match("%.epub$") and lfs.attributes(path, "mode") == "file"
                and DocSettings:hasSidecarFile(path) then
            local summary = DocSettings:open(path):readSetting("summary")
            if summary and summary.status == "complete" then table.insert(finished, name) end
        end
    end
    local month = os.date("*t")
    local target = string.format("%s/%s/%04d-%02d %s", dir, READ_FOLDER, month.year, month.month, MONTHS[month.month])
    local moved = 0
    for _, name in ipairs(finished) do
        if self:moveDocument(dir .. "/" .. name, target .. "/" .. name) then moved = moved + 1 end
    end
    return moved
end

-- One-off moves the PC asked for ("from\tto" lines), only for EPUBs going straight into the articles folder.
function KindleDrop:runMoves(dir)
    local content = readSmall(self.moves, 64 * 1024)
    if content == nil then
        if lfs.attributes(self.moves, "mode") then os.remove(self.moves) end
        return 0
    end
    os.remove(self.moves)
    local moved, count = 0, 0
    for from, to in content:gmatch("([^\t\n]+)\t([^\t\n]+)\n") do
        count = count + 1
        if count > MAX_MOVES then break end
        local name = to:sub(#dir + 2)
        if from:sub(1, 8) == "/mnt/us/" and not from:find("/%.%.") and from:lower():match("%.epub$")
                and inside(to, dir) and name ~= "" and not name:find("/") and name:sub(1, 1) ~= "."
                and name:lower():match("%.epub$") and self:moveDocument(from, to) then
            moved = moved + 1
        end
    end
    return moved
end

function KindleDrop:addToMainMenu(items)
    if not self.tick then return end
    items.kindle_drop_refresh = {
        text = "Kindle Drop auto refresh",
        checked_func = function() return self.enabled end,
        callback = function()
            self.enabled = not self.enabled
            G_reader_settings:saveSetting("kindle_drop_auto_refresh", self.enabled)
            UIManager:unschedule(self.tick)
            if self.enabled and not self.suspended then UIManager:scheduleIn(1, self.tick) end
        end,
    }
    items.kindle_drop_archive = {
        text = "Kindle Drop: file finished articles in " .. READ_FOLDER,
        checked_func = function() return self.archiving end,
        callback = function()
            self.archiving = not self.archiving
            G_reader_settings:saveSetting("kindle_drop_archive", self.archiving)
        end,
    }
end

function KindleDrop:onSuspend()
    self.suspended = true
    if self.tick then UIManager:unschedule(self.tick) end
end

function KindleDrop:onResume()
    self.suspended = false
    if self.tick and self.enabled and not self.closed then
        UIManager:unschedule(self.tick)
        UIManager:scheduleIn(1, self.tick)
    end
end

function KindleDrop:onCloseWidget()
    self.closed = true
    if self.tick then UIManager:unschedule(self.tick) end
end

return KindleDrop
