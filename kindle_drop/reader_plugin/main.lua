-- Kindle Drop managed auto-refresh plugin v1
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local UIManager = require("ui/uimanager")
local DataStorage = require("datastorage")
local logger = require("logger")

local KindleDrop = WidgetContainer:extend{
    name = "kindle_drop_refresh",
    is_doc_only = false,
}

function KindleDrop:init()
    -- ReaderUI also instantiates plugins. Do no polling while reading a book.
    if self.ui.document then return end
    self.enabled = not G_reader_settings:isFalse("kindle_drop_auto_refresh")
    self.marker = DataStorage:getSettingsDir() .. "/kindle-drop.refresh"
    self.ui.menu:registerToMainMenu(self)
    self.tick = function()
        if self.closed or self.suspended or not self.enabled then return end
        local ok, err = pcall(self.checkDelivery, self)
        if not ok then logger.warn("Kindle Drop refresh:", err) end
        UIManager:scheduleIn(5, self.tick)
    end
    if self.enabled then UIManager:scheduleIn(1, self.tick) end
end

function KindleDrop:checkDelivery()
    -- Defer while a menu/dialog covers the browser; never change folders or sorting.
    if self.ui.tearing_down or UIManager:getTopmostVisibleWidget() ~= self.ui then return end
    local file = io.open(self.marker, "rb")
    if not file then return end
    local content = file:read(4097)
    file:close()
    if not content or #content > 4096 then return end
    local nonce, folder = content:match("^(%x+)\n([^\n]+)\n$")
    if not nonce or #nonce ~= 32 or nonce == self.seen then return end
    local chooser = self.ui.file_chooser
    if not chooser or chooser.path:gsub("/+$", "") ~= folder:gsub("/+$", "") then return end
    chooser:clearSortingCache()
    self.ui:onRefresh()
    self.seen = nonce
    logger.info("Kindle Drop: refreshed delivered book folder")
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
