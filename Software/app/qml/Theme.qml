pragma Singleton
import QtQuick

QtObject {
    // Driven by Main.qml from the window's current size relative to the
    // 960x600 design baseline, clamped so a maximised/fullscreen window
    // scales everything up (and the enforced 720x480 minimum scales it
    // down a little) instead of every size but the baseline looking off.
    property real uiScale: 1.0

    readonly property color colorBackground: "#0B0B0F"
    // Flat idle/waiting backdrop (Background.qml) -- deliberately a plain
    // neutral grey, not a gradient or anything derived from artwork, since
    // there's no track playing yet to derive anything from.
    readonly property color colorIdleBackground: "#303034"
    readonly property color colorSurface: "#1C1C22"
    readonly property color colorSurfaceElevated: "#26262E"
    readonly property color colorBorder: "#33333B"
    readonly property color colorTextPrimary: "#F5F5F7"
    readonly property color colorTextSecondary: "#9A9AA2"
    readonly property color colorAccent: "#0A84FF"

    readonly property color colorStatusGreen: "#32D74B"
    readonly property color colorStatusAmber: "#FF9F0A"
    readonly property color colorStatusRed: "#FF453A"
    readonly property color colorStatusGray: "#8E8E93"

    // Segoe UI Variable is Windows 11's modern system font; Qt falls back
    // to the platform default sans automatically if it isn't found (e.g.
    // on the Pi), so no explicit fallback chain is needed here.
    readonly property string fontFamily: "Segoe UI Variable Display"

    readonly property int spacingXs: Math.round(4 * uiScale)
    readonly property int spacingSm: Math.round(8 * uiScale)
    readonly property int spacingMd: Math.round(16 * uiScale)
    readonly property int spacingLg: Math.round(24 * uiScale)
    readonly property int spacingXl: Math.round(32 * uiScale)

    readonly property int radiusCard: Math.round(20 * uiScale)
    readonly property int radiusArt: Math.round(28 * uiScale)
    readonly property int radiusControl: Math.round(12 * uiScale)
    readonly property int radiusPill: 999

    readonly property int durationFast: 150
    readonly property int durationBase: 250
    readonly property int durationSlow: 400
}
