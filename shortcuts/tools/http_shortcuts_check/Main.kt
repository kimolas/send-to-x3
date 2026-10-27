// Runs HTTP Shortcuts' own import pipeline (ImportMigrator -> Gson -> defaults -> validate,
// as in Importer.importFromJSON) on an import file, then renders the shortcut's request
// for some share cases with the app's own placeholder code (Variables.kt) and variable
// encoding (VariableManager.encodeValue). Prints one JSON object.
//
// Usage: Main <import.json> <cases.json>
//   cases.json: [{"text": "...", "title": "...", "values": {"<variable key>": "..."}}]

package sendtox3check

import ch.rmy.android.http_shortcuts.import_export.ImportExportDefaultsProvider
import ch.rmy.android.http_shortcuts.import_export.ImportMigrator
import ch.rmy.android.http_shortcuts.import_export.models.ImportExportBase
import ch.rmy.android.http_shortcuts.variables.ResolvedVariableValues
import ch.rmy.android.http_shortcuts.variables.Variables
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonParser
import java.io.File
import org.json.JSONObject

data class Case(val text: String = "", val title: String = "", val values: Map<String, String> = emptyMap())

fun main(args: Array<String>) {
    val gson = Gson()
    val raw = JsonParser.parseString(File(args[0]).readText())
    val migrated = ImportMigrator().migrate(raw)
    val base = ImportExportDefaultsProvider().applyDefaults(gson.fromJson(migrated, ImportExportBase::class.java))
    base.validate() // throws IllegalArgumentException with the app's own message

    val variables = base.variables.orEmpty()
    val shortcut = base.categories!!.single().shortcuts!!.single()

    // Which global variables the shortcut uses (VariableResolver.findResolvableVariableIdentifiers
    // for an HTTP shortcut with a custom body, including scripting).
    val fields = listOfNotNull(shortcut.url, shortcut.bodyContent) + shortcut.headers.orEmpty().flatMap {
        listOfNotNull(it.key, it.value)
    }
    val scripts = listOfNotNull(shortcut.codeOnPrepare, shortcut.codeOnSuccess, shortcut.codeOnFailure)
    val identifiers = fields.flatMap { Variables.findResolvableVariableIdentifiers(it) } +
        scripts.flatMap { Variables.findResolvableVariableIdentifiersFromJS(it) + Variables.findResolvableVariableIdentifiers(it) }
    val byId = variables.associateBy { it.id }
    val byKey = variables.associateBy { it.key }
    val usedIds = identifiers.mapNotNull { id ->
        (id.globalVariableId?.let { byId[it] } ?: id.variableKey?.let { byKey[it] })?.id
    }.toSet()
    val unresolved = identifiers.filter { id ->
        (id.globalVariableId?.let { byId[it] } ?: id.variableKey?.let { byKey[it] }) == null
    }.map { it.value }

    val cases = gson.fromJson(File(args[1]).readText(), Array<Case>::class.java).orEmpty()
    val rendered = cases.map { case ->
        val values = variables.associate { v ->
            val value = when {
                v.isShareText == true && v.isShareTitle == true -> "${case.title} - ${case.text}"
                v.isShareTitle == true -> case.title
                v.isShareText == true -> case.text
                else -> case.values[v.key!!] ?: v.value.orEmpty()
            }
            // VariableManager.encodeValue
            var encoded = value
            if (v.jsonEncode == true) encoded = JSONObject.quote(encoded).drop(1).dropLast(1)
            v.id!! to encoded
        }
        val resolved = ResolvedVariableValues(
            globalVariableValues = values,
            localVariablesValues = emptyMap(),
            globalVariableKeysToIds = variables.associate { it.key!! to it.id!! },
        )
        fun sub(s: String?) = Variables.rawPlaceholdersToResolvedValues(s.orEmpty(), resolved)
        mapOf(
            "url" to sub(shortcut.url),
            "headers" to shortcut.headers.orEmpty().associate { sub(it.key) to sub(it.value) },
            "body" to sub(shortcut.bodyContent),
        )
    }

    val out = mapOf(
        "version" to base.version,
        "compatibilityVersion" to base.compatibilityVersion,
        "shortcut" to mapOf(
            "name" to shortcut.name,
            "executionType" to shortcut.executionType,
            "method" to shortcut.method,
            "requestBodyType" to shortcut.requestBodyType,
            "contentType" to shortcut.contentType,
            "timeout" to shortcut.timeout,
            "waitForInternet" to shortcut.waitForInternet,
            "launcherShortcut" to shortcut.launcherShortcut,
            "excludeFromFileSharing" to shortcut.excludeFromFileSharing,
            "responseHandling" to shortcut.responseHandling,
        ),
        "variables" to variables.map {
            mapOf("key" to it.key, "type" to it.type, "jsonEncode" to it.jsonEncode, "isShareText" to it.isShareText,
                  "isShareTitle" to it.isShareTitle, "isExcludeValueFromExport" to it.isExcludeValueFromExport)
        },
        "usedVariableKeys" to variables.filter { it.id in usedIds }.map { it.key },
        "unresolvedIdentifiers" to unresolved,
        "isTextShareTarget" to variables.any { (it.isShareText == true || it.isShareTitle == true) && it.id in usedIds },
        "rendered" to rendered,
    )
    println(GsonBuilder().disableHtmlEscaping().serializeNulls().create().toJson(out))
}
