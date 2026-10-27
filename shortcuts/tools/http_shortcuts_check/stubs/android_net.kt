@file:Suppress("unused")

package android.net

/** Only what ImportMigrator's WorkingDirectoryMigration (legacy files) uses. */
class Uri(private val value: String) {
    val lastPathSegment: String?
        get() = value.trimEnd('/').substringAfterLast('/').ifEmpty { null }

    override fun toString(): String = value
}
