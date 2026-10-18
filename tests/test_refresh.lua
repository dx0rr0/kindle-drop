-- Run with Lua/LuaJIT: luajit tests/test_refresh.lua kindle_drop/reader_plugin/main.lua
local tasks, top, refreshes, marker, moves = {}, nil, 0, nil, nil
local manager = {}
function manager:scheduleIn(_, action) tasks[action] = true end
function manager:unschedule(action) tasks[action] = nil end
function manager:getTopmostVisibleWidget() return top end
local settings = {}
G_reader_settings = {
    isFalse = function(_, key) return settings[key] == false end,
    readSetting = function(_, key) return settings[key] end,
    saveSetting = function(_, key, value) settings[key] = value end,
}
-- A small file system: path -> "file" or "directory", and each book's reading status.
local fs, status, history, collections, sidecars = {}, {}, {}, {}, {}
local function add(path, mode) fs[path] = mode end
local lfs = {
    attributes = function(path, what) assert(what == "mode"); return fs[path] end,
    dir = function(path)
        local names = {}
        for p in pairs(fs) do
            local parent, name = p:match("^(.*)/([^/]+)$")
            if parent == path then table.insert(names, name) end
        end
        table.sort(names)
        local i = 0
        return function() i = i + 1; return names[i] end
    end,
}
local DocSettings = {
    hasSidecarFile = function(_, path) return status[path] ~= nil end,
    open = function(_, path) return {readSetting = function(_, key) assert(key == "summary"); return {status = status[path]} end} end,
    updateLocation = function(from, to) status[to], status[from] = status[from], nil; table.insert(sidecars, to) end,
}
local container = {}
function container:extend(value) return value end
package.preload["ui/widget/container/widgetcontainer"] = function() return container end
package.preload["ui/uimanager"] = function() return manager end
package.preload["datastorage"] = function() return {getSettingsDir = function() return "settings" end} end
package.preload["logger"] = function() return {warn = function(...) print("warn", ...) end, info = function() end} end
package.preload["docsettings"] = function() return DocSettings end
package.preload["readhistory"] = function() return {updateItem = function(_, from, to) history[from] = to end} end
package.preload["readcollection"] = function() return {updateItem = function(_, from, to) collections[from] = to end} end
package.preload["libs/libkoreader-lfs"] = function() return lfs end
package.preload["util"] = function() return {makePath = function(path)
    local built = ""
    for part in path:gmatch("[^/]+") do built = built .. "/" .. part; fs[built] = fs[built] or "directory" end
end} end
local Plugin = dofile(arg[1] or "kindle_drop/reader_plugin/main.lua")
local original_open, original_remove = io.open, os.remove
io.open = function(path)
    local content
    if path == "settings/kindle-drop.refresh" then content = marker
    elseif path == "settings/kindle-drop.moves" then content = moves
    else error("unexpected open " .. path) end
    if not content then return nil end
    return {read = function(_, limit) return content:sub(1, limit) end, close = function() end}
end
os.remove = function(path) assert(path == "settings/kindle-drop.moves"); moves = nil end
local function instance(doc)
    local ui = {document = doc, menu = {registerToMainMenu = function() end},
        file_chooser = {path = "/mnt/us/books", clearSortingCache = function() end}}
    function ui:onRefresh() refreshes = refreshes + 1 end
    function ui:moveFile(from, folder)
        assert(fs[folder] == "directory")
        local to = folder .. "/" .. from:match("[^/]+$")
        fs[to], fs[from] = fs[from], nil
        return true
    end
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

-- An article delivered into Artículos refreshes the parent folder too, where its item count shows.
p.ui.file_chooser.path = "/mnt/us/books"
marker = string.rep("d", 32) .. "\n/mnt/us/books/Artículos\n/mnt/us/books/Artículos\n"
p.tick()
assert(refreshes == 4, "Parent of the delivery folder refreshes")
assert(settings.kindle_drop_articles == "/mnt/us/books/Artículos", "The articles folder is remembered")

-- Finished articles are filed under Leídos/<month>, with sidecar, History and collections following.
local dir = "/mnt/us/books/Artículos"
add("/mnt/us", "directory"); add("/mnt/us/books", "directory"); add(dir, "directory")
add(dir .. "/done.epub", "file"); status[dir .. "/done.epub"] = "complete"
add(dir .. "/reading.epub", "file"); status[dir .. "/reading.epub"] = "reading"
add(dir .. "/new.epub", "file")
add(dir .. "/notes.txt", "file"); status[dir .. "/notes.txt"] = "complete"
add("/mnt/us/books/book.epub", "file"); status["/mnt/us/books/book.epub"] = "complete"
p.ticks = 100 -- the next tick is an archive tick
p.tick()
local month = os.date("*t")
local names = { "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre" }
local filed = string.format("%s/Leídos/%04d-%02d %s/done.epub", dir, month.year, month.month, names[month.month])
assert(fs[filed] == "file" and fs[dir .. "/done.epub"] == nil, "Finished article filed")
assert(status[filed] == "complete" and history[dir .. "/done.epub"] == filed and collections[dir .. "/done.epub"] == filed)
assert(fs[dir .. "/reading.epub"] and fs[dir .. "/new.epub"] and fs[dir .. "/notes.txt"], "Unfinished articles and other files stay")
assert(fs["/mnt/us/books/book.epub"], "Books outside the articles folder are never moved")
assert(refreshes == 5, "The browser shows the move")
p.tick()
assert(refreshes == 5, "Archiving waits about a minute between looks")

-- A finished copy never replaces one already filed with the same name.
add(dir .. "/done.epub", "file"); status[dir .. "/done.epub"] = "complete"
p.ticks = 100
p.tick()
assert(fs[dir .. "/done.epub"] == "file", "Existing filed copy is not overwritten")

-- Moves queued by the PC: only EPUBs going straight into the articles folder.
add("/mnt/us/books/old.epub", "file")
add("/mnt/us/books/keep.epub", "file")
moves = "/mnt/us/books/old.epub\t" .. dir .. "/old.epub\n"
    .. "/mnt/us/books/keep.epub\t/mnt/us/books/elsewhere/keep.epub\n"
    .. "/mnt/us/books/keep.epub\t" .. dir .. "/sub/keep.epub\n"
    .. "/etc/passwd\t" .. dir .. "/passwd.epub\n"
    .. "/mnt/us/books/../keep.epub\t" .. dir .. "/keep.epub\n"
p.ticks = 100
p.tick()
assert(fs[dir .. "/old.epub"] == "file" and fs["/mnt/us/books/old.epub"] == nil, "Queued move ran")
assert(fs["/mnt/us/books/keep.epub"] == "file", "Moves outside the articles folder are refused")
assert(moves == nil, "The request is consumed once")
settings.kindle_drop_archive = false
p.archiving = false
add(dir .. "/later.epub", "file"); status[dir .. "/later.epub"] = "complete"
p.ticks = 100
p.tick()
assert(fs[dir .. "/later.epub"], "Archiving can be switched off")

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
menu.kindle_drop_archive.callback()
assert(p.archiving and settings.kindle_drop_archive == true)
p:onCloseWidget()
assert(not tasks[p.tick])
p.tick()
assert(not tasks[p.tick], "Closed browser cannot leave polling behind")
local reader = instance({})
assert(reader.tick == nil, "No polling while reading")
reader:onSuspend()
reader:onResume()
reader:onCloseWidget()
io.open, os.remove = original_open, original_remove
print("PASS: delivery coalescing, dialog deferral, folder preservation, size bounds, article filing, queued moves, suspend, disable, teardown and reading isolation")
