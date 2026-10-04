-- Run with Lua/LuaJIT: luajit tests/test_refresh.lua kindle_drop/reader_plugin/main.lua
local tasks, top, refreshes, marker = {}, nil, 0, nil
local manager = {}
function manager:scheduleIn(_, action) tasks[action] = true end
function manager:unschedule(action) tasks[action] = nil end
function manager:getTopmostVisibleWidget() return top end
local settings = {}
G_reader_settings = {
    isFalse = function(_, key) return settings[key] == false end,
    saveSetting = function(_, key, value) settings[key] = value end,
}
local container = {}
function container:extend(value) return value end
package.preload["ui/widget/container/widgetcontainer"] = function() return container end
package.preload["ui/uimanager"] = function() return manager end
package.preload["datastorage"] = function() return {getSettingsDir = function() return "settings" end} end
package.preload["logger"] = function() return {warn = function() end, info = function() end} end
local Plugin = dofile(arg[1] or "kindle_drop/reader_plugin/main.lua")
local original_open = io.open
io.open = function(path)
    assert(path == "settings/kindle-drop.refresh")
    if not marker then return nil end
    return {read = function(_, limit) return marker:sub(1, limit) end, close = function() end}
end
local function instance(doc)
    local ui = {document = doc, menu = {registerToMainMenu = function() end},
        file_chooser = {path = "/mnt/us/books", clearSortingCache = function() end}}
    function ui:onRefresh() refreshes = refreshes + 1 end
    local p = setmetatable({ui = ui}, {__index = Plugin})
    p:init()
    return p
end
local p = instance(nil)
top = p.ui
marker = string.rep("a", 32) .. "\n/mnt/us/books\n"
p.tick()
assert(refreshes == 1)
p.tick()
assert(refreshes == 1, "No repeated screen refresh for the same delivery")
marker = string.rep("b", 32) .. "\n/mnt/us/books\n"
top = {}
p.tick()
assert(refreshes == 1, "Menus defer refresh")
top = p.ui
p.tick()
assert(refreshes == 2, "Deferred refresh runs after menu closes")
marker = string.rep("c", 32) .. "\n/mnt/us/elsewhere\n"
p.tick()
assert(refreshes == 2, "No jumping to another folder")
p.ui.file_chooser.path = "/mnt/us/elsewhere/"
p.tick()
assert(refreshes == 3)
marker = string.rep("z", 5000)
p.tick()
assert(refreshes == 3, "Oversized markers ignored")
p:onSuspend()
assert(not tasks[p.tick])
p.tick()
assert(not tasks[p.tick], "No rescheduling in suspend")
p:onResume()
assert(tasks[p.tick])
local menu = {}
p:addToMainMenu(menu)
menu.kindle_drop_refresh.callback()
assert(not tasks[p.tick] and settings.kindle_drop_auto_refresh == false)
menu.kindle_drop_refresh.callback()
assert(tasks[p.tick])
p:onCloseWidget()
assert(not tasks[p.tick])
p.tick()
assert(not tasks[p.tick], "Closed browser cannot leave polling behind")
local reader = instance({})
assert(reader.tick == nil, "No polling while reading")
reader:onSuspend()
reader:onResume()
reader:onCloseWidget()
io.open = original_open
print("PASS: delivery coalescing, dialog deferral, folder preservation, size bounds, suspend, disable, teardown and reading isolation")
