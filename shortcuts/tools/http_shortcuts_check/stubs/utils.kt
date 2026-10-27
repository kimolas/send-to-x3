// Copied from HTTP Shortcuts app/src/main/kotlin/ch/rmy/android/http_shortcuts/utils
// (ColorUtil.colorIntToHexString, Validation.isValidInHeaderName/Value).
@file:Suppress("unused")

package ch.rmy.android.http_shortcuts.utils

object ColorUtil {
    fun Int.colorIntToHexString(): String =
        String.format("%06x", this and 0xffffff).uppercase()
}

object Validation {
    fun isValidInHeaderName(c: Char): Boolean =
        c in '!'..'~'

    fun isValidInHeaderValue(c: Char): Boolean =
        (c > '\u001f' || c == '\t') && c < '\u007f'
}
