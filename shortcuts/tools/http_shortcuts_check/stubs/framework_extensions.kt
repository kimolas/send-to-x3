// Copied from HTTP Shortcuts (app/src/main/kotlin/ch/rmy/android/framework/extensions and
// framework/src/main/kotlin/ch/rmy/android/framework/extensions), logging made silent.
@file:Suppress("unused")

package ch.rmy.android.framework.extensions

import ch.rmy.android.framework.utils.UUIDUtils

fun Any.logInfo(message: String) {
    System.err.println("logInfo: $message")
}

fun Any.logException(e: Throwable) {
    System.err.println("logException: $e")
}

inline fun <T> Any.tryOrLog(block: () -> T): T? =
    try {
        block()
    } catch (e: Throwable) {
        logException(e)
        null
    }

fun String.isUUID() =
    UUIDUtils.isUUID(this)

fun String.isInt() =
    toIntOrNull() != null

@OptIn(ExperimentalStdlibApi::class)
inline fun <T> Collection<T>.hasDuplicatesBy(getKey: (T) -> Any?): Boolean =
    !isEmpty() && map(getKey).toSet().size != size
