@file:Suppress("unused")

package androidx.core.net

fun String.toUri(): android.net.Uri = android.net.Uri(this)
