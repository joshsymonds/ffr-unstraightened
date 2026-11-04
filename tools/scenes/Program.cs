using System.Text.Json;
using Newtonsoft.Json.Linq;
using CUE4Parse.Compression;
using CUE4Parse.Encryption.Aes;
using CUE4Parse.FileProvider;
using CUE4Parse.MappingsProvider.Usmap;
using CUE4Parse.UE4.Assets.Exports.Engine;
using CUE4Parse.UE4.Assets.Exports.Internationalization;
using CUE4Parse.UE4.Assets.Objects;
using CUE4Parse.UE4.Assets.Objects.Properties;
using CUE4Parse.UE4.Objects.Core.i18N;
using CUE4Parse.UE4.Objects.Core.Misc;
using CUE4Parse.UE4.Versions;

static class Program
{
    const string Usage = "usage: scenes <paks_dir> <usmap> <oodle_lib> <out.json>";

    sealed record LineTable(string Id, List<SceneLine> Lines);
    sealed record SceneLine(string Namespace, string Key, string? Speaker, int Group);
    sealed record SceneRef(string Table, string? Sheet, string Event, Dictionary<string, string> Args);
    sealed record Startup(string Event, int Map, string Condition);
    sealed record MapLevel(int Map, string Level);
    sealed record PlacedEvent(string Event, string Condition);
    sealed record Placement(string Level, string Class, int? Unit, double X, double Y, double Z, List<PlacedEvent> Events);
    sealed record CueSheet(string Name, List<string> Cues);
    sealed record Output(List<LineTable> Tables, List<SceneRef> References, List<CueSheet> Sheets, List<Startup> Startup, List<MapLevel> Maps, List<Placement> Placements);

    static int Main(string[] args)
    {
        if (args.Length != 4)
        {
            Console.Error.WriteLine(Usage);
            return 2;
        }

        try
        {
            return Run(args[0], args[1], args[2], args[3]);
        }
        catch (Exception e)
        {
            Console.WriteLine(e.GetType().Name);
            return 1;
        }
    }

    static int Run(string paksDir, string usmap, string oodle, string outPath)
    {
        OodleHelper.Initialize(oodle);
        var provider = new DefaultFileProvider(paksDir, SearchOption.TopDirectoryOnly, new VersionContainer(EGame.GAME_UE5_6), StringComparer.OrdinalIgnoreCase);
        provider.MappingsContainer = new FileUsmapTypeMappingsProvider(usmap);
        provider.Initialize();
        // The containers are unencrypted; submitting the zero key is what mounts them.
        provider.SubmitKey(new FGuid(), new FAesKey(new byte[32]));
        provider.PostMount();

        var failed = 0;
        var namespaces = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);
        var tables = new List<LineTable>();
        var references = new List<SceneRef>();
        var startup = new List<Startup>();
        var startupSeen = new HashSet<Startup>();
        var maps = new List<MapLevel>();
        var mapSeen = new HashSet<MapLevel>();

        var datatables = provider.Files.Keys
            .Where(p => p.StartsWith("FFRS/Content/Datatable/", StringComparison.OrdinalIgnoreCase) && p.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase))
            .OrderBy(p => p, StringComparer.Ordinal)
            .ToList();

        foreach (var path in datatables)
        {
            try
            {
                foreach (var export in provider.LoadPackage(path).GetExports())
                {
                    if (export is not UDataTable table) continue;

                    if (table.RowStructName is "TalkEventRowData" or "ChatRowData")
                    {
                        var lines = new List<SceneLine>();
                        // A row with a TalkId of 0 or more opens a talk group; the lines after it belong to it.
                        var group = -1;
                        foreach (var row in table.RowMap.Values)
                        {
                            var talkId = PropertyTag<IntProperty>(row.Properties, "TalkId")?.Value ?? -1;
                            if (talkId >= 0) group = talkId;
                            var history = PropertyTag<TextProperty>(row.Properties, "ContentText")?.Value?.TextHistory;
                            var speaker = PropertyTag<NameProperty>(row.Properties, "Speaker")?.Value.Text;
                            if (speaker is null or "" or "None") speaker = null;

                            if (history is FTextHistory.StringTableEntry entry)
                            {
                                var ns = TableNamespace(provider, namespaces, entry.TableId.Text);
                                if (ns is not null) lines.Add(new SceneLine(ns, entry.Key, speaker, group));
                            }
                            else if (history is FTextHistory.Base b && !string.IsNullOrEmpty(b.Key))
                            {
                                lines.Add(new SceneLine(b.Namespace ?? "", b.Key, speaker, group));
                            }
                        }
                        tables.Add(new LineTable(path[..^".uasset".Length], lines));
                    }
                    else if (table.RowStructName is "TalkEventData" or "ChatEventData")
                    {
                        var field = table.RowStructName == "TalkEventData" ? "TalkEventDataTable" : "TalkDataTable";
                        foreach (var (name, row) in table.RowMap)
                        {
                            var target = PropertyTag<SoftObjectProperty>(row.Properties, field)?.Value.AssetPathName.Text;
                            if (string.IsNullOrEmpty(target) || target == "None") continue;
                            var sheet = PropertyTag<StrProperty>(row.Properties, "CueSheetName")?.Value;
                            // Soft paths can differ in case from the package path; use the package's own spelling.
                            var file = AssetFile(target);
                            if (provider.Files.TryGetValue(file, out var package)) file = package.Path;
                            var args = new Dictionary<string, string>();
                            if (row.Properties.FirstOrDefault(t => t.Name.Text == "ArgList") is { } argList
                                && JToken.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(argList.Tag)) is JArray argItems)
                            {
                                foreach (var item in argItems.OfType<JObject>())
                                {
                                    if (item["Key"]?.ToString() is { Length: > 0 } argKey) args[argKey] = item["Value"]?.ToString() ?? "";
                                }
                            }
                            references.Add(new SceneRef(file[..^".uasset".Length], string.IsNullOrEmpty(sheet) ? null : sheet, name.Text, args));
                        }
                    }
                    else if (table.RowStructName == "MAP_DATA")
                    {
                        // A map plays each startup event on load when the save meets its condition.
                        foreach (var row in table.RowMap.Values)
                        {
                            var map = PropertyTag<IntProperty>(row.Properties, "ID")?.Value;
                            if (map is null) continue;
                            var level = PropertyTag<SoftObjectProperty>(row.Properties, "Level")?.Value.AssetPathName.Text;
                            if (!string.IsNullOrEmpty(level) && level != "None" && mapSeen.Add(new MapLevel(map.Value, level))) maps.Add(new MapLevel(map.Value, level));
                            if (PropertyTag<ArrayProperty>(row.Properties, "startupEventList") is not { Value: not null } list) continue;
                            foreach (var element in list.Value.Properties)
                            {
                                if (element is not StructProperty { Value.StructType: FStructFallback entry }) continue;
                                var eventId = PropertyTag<NameProperty>(entry.Properties, "EventId")?.Value.Text ?? PropertyTag<StrProperty>(entry.Properties, "EventId")?.Value;
                                var condition = PropertyTag<StrProperty>(entry.Properties, "Condition")?.Value ?? "";
                                if (string.IsNullOrEmpty(eventId) || eventId == "None") continue;
                                var item = new Startup(eventId, map.Value, condition);
                                if (startupSeen.Add(item)) startup.Add(item);
                            }
                        }
                    }
                }
            }
            catch (Exception)
            {
                failed++;
            }
        }

        // Every placed actor that starts events: town NPCs and the triggers of story scenes, with the root position.
        var placements = new List<Placement>();
        var levels = provider.Files.Keys
            .Where(p => p.StartsWith("FFRS/Content/Map/", StringComparison.OrdinalIgnoreCase) && p.EndsWith(".umap", StringComparison.OrdinalIgnoreCase))
            .OrderBy(p => p, StringComparer.Ordinal)
            .ToList();
        foreach (var path in levels)
        {
            try
            {
                var exports = provider.LoadPackage(path).GetExports().ToList();
                foreach (var export in exports)
                {
                    var props = JObject.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(export))["Properties"] as JObject;
                    if (props is null) continue;
                    var events = PlacedEvents(props, "").Distinct().ToList();
                    if (events.Count == 0) continue;
                    var rootName = ((string?)props["RootComponent"]?["ObjectName"])?.TrimEnd('\'').Split('.').Last();
                    var root = rootName is null ? null : exports.FirstOrDefault(c => c.Name == rootName && c.Outer?.Name == export.Name);
                    var at = root is null ? null : JObject.Parse(Newtonsoft.Json.JsonConvert.SerializeObject(root))["Properties"]?["RelativeLocation"];
                    if (at is null) continue;
                    placements.Add(new Placement(
                        path[..^".umap".Length], export.Class?.Name.ToString() ?? "", (int?)props["m_MapUnitId"],
                        (double)at["X"]!, (double)at["Y"]!, (double)at["Z"]!, events));
                }
            }
            catch (Exception)
            {
                failed++;
            }
        }

        var sheetPaths = provider.Files.Keys
            .Where(p => (p.StartsWith("FFRS/Content/Sound/", StringComparison.OrdinalIgnoreCase) || p.StartsWith("FFRS/Content/L10N/en/", StringComparison.OrdinalIgnoreCase)) && p.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase))
            .OrderBy(p => p.StartsWith("FFRS/Content/L10N/", StringComparison.OrdinalIgnoreCase))
            .ThenBy(p => p, StringComparer.Ordinal)
            .ToList();

        var sheets = new List<CueSheet>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var path in sheetPaths)
        {
            try
            {
                foreach (var export in provider.LoadPackage(path).GetExports())
                {
                    if (export.ExportType != "SoundAtomCueSheet") continue;
                    var name = PropertyTag<StrProperty>(export.Properties, "CueSheetName")?.Value ?? "";
                    if (!seen.Add(name)) continue;

                    var cues = new List<string>();
                    if (PropertyTag<ArrayProperty>(export.Properties, "CueInfos") is { Value: not null } infos)
                    {
                        foreach (var element in infos.Value.Properties)
                        {
                            if (element is StructProperty { Value.StructType: FStructFallback cue })
                                cues.Add(PropertyTag<StrProperty>(cue.Properties, "Name")?.Value ?? "");
                        }
                    }
                    sheets.Add(new CueSheet(name, cues));
                }
            }
            catch (Exception)
            {
                failed++;
            }
        }

        if (failed > 0)
        {
            Console.WriteLine($"failed packages {failed}");
            return 1;
        }

        var json = JsonSerializer.Serialize(
            new Output(tables, references, sheets, startup, maps, placements),
            new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true });
        File.WriteAllText(outPath, json);

        Console.WriteLine($"tables {tables.Count} references {references.Count} sheets {sheets.Count} startup {startup.Count} maps {maps.Count} placements {placements.Count}");
        return 0;
    }

    static string AssetFile(string softPath) => "FFRS/Content/" + softPath["/Game/".Length..].Split('.')[0] + ".uasset";

    // The (condition, event id) pairs an actor's properties list, each under its nearest enclosing condition.
    static IEnumerable<PlacedEvent> PlacedEvents(JToken token, string condition)
    {
        if (token is JObject o)
        {
            var own = o["Condition"]?.Type == JTokenType.String ? (string)o["Condition"]! : condition;
            if (o["EventId"]?.Type == JTokenType.String && (string)o["EventId"]! is { Length: > 0 } id && id != "None") yield return new PlacedEvent(id, own);
            foreach (var p in o.Properties())
                foreach (var e in PlacedEvents(p.Value, own)) yield return e;
        }
        else if (token is JArray a)
        {
            foreach (var v in a)
                foreach (var e in PlacedEvents(v, condition)) yield return e;
        }
    }

    static string? TableNamespace(DefaultFileProvider provider, Dictionary<string, string?> cache, string tableId)
    {
        if (cache.TryGetValue(tableId, out var cached)) return cached;
        string? ns = null;
        foreach (var export in provider.LoadPackage(AssetFile(tableId)).GetExports())
        {
            if (export is UStringTable strings)
            {
                ns = strings.StringTable.TableNamespace;
                break;
            }
        }
        cache[tableId] = ns;
        return ns;
    }

    static T? PropertyTag<T>(IEnumerable<FPropertyTag> properties, string name) where T : class =>
        properties.FirstOrDefault(p => p.Name.Text == name)?.Tag as T;
}
