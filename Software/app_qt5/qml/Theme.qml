pragma Singleton
import QtQuick 2.15

QtObject {
    // Driven by Main.qml from the window's current size relative to the
    // 960x600 design baseline, clamped so a maximised/fullscreen window
    // scales everything up (and the enforced 720x480 minimum scales it
    // down a little) instead of every size but the baseline looking off.
    property real uiScale: 1.0

    readonly property color colorBackground: "#0B0B0F"
    // Flat idle/waiting backdrop (Background.qml) -- pure black, no
    // gradient or anything derived from artwork, since there's no track
    // playing yet to derive anything from. Zero WH build deliberately
    // black rather than the Pi 5's neutral grey: this is the screen that
    // sits on-air the most (waiting for a session), so it's the one most
    // worth being genuinely free of GPU work, not just visually plain.
    readonly property color colorIdleBackground: "#000000"
    readonly property color colorSurface: "#1C1C22"
    readonly property color colorSurfaceElevated: "#26262E"
    readonly property color colorBorder: "#33333B"
    readonly property color colorTextPrimary: "#F5F5F7"
    readonly property color colorTextSecondary: "#9A9AA2"
    // Now-playing text (title/artist/lyrics) swaps to these over light
    // album art -- TrackController.textIsDark decides which pair applies;
    // see encoder.legible_text_is_dark, which mirrors these two hex values
    // by hand to pick between them.
    readonly property color colorTextPrimaryOnLight: "#15151A"
    readonly property color colorTextSecondaryOnLight: "#4A4A54"

    // The artist/album line under the now-playing title, and *only* that --
    // deliberately its own pair rather than reusing colorTextSecondary.
    //
    // textIsDark picks its light/dark polarity by comparing the two primary
    // inks above against the artwork (encoder.legible_text_is_dark mirrors
    // those two hex values, not these), so the secondary pair was never the
    // colour that decision was actually validated for -- and it is a good
    // deal dimmer. Running the same maths over every grey backdrop, with
    // Background.qml's 0.35 scrim folded in: the primary pair never drops
    // below 4.1:1, while colorTextSecondary bottoms out at 1.6:1 around a
    // mid-bright cover. 1.6:1 is not "a bit hard to read", it is text the
    // same brightness as what is behind it, which is exactly the report
    // this pair was changed for. These two hold about 3.3:1 at that same
    // worst point.
    //
    // They sit close to the primary inks on purpose. The hierarchy between
    // the title and this line is carried by 28px Bold against 16px Regular,
    // which is plenty; spending it on brightness as well is what cost the
    // line its legibility. LyricsPanel keeps the original secondary pair --
    // its sung/pending distinction is genuinely a colour signal and is read
    // against a known neighbour rather than against arbitrary artwork.
    readonly property color colorTextDetail: "#DCDCE4"
    readonly property color colorTextDetailOnLight: "#26262F"

    // Glyph outline (Text.style/styleColor) for the now-playing text over
    // artwork, in whichever ink textIsDark did *not* choose.
    //
    // Colour alone cannot finish the job, because the polarity it is chosen
    // with can be wrong in the first place: legible_text_is_dark averages
    // the whole cover into one colour, but the text sits over the right-hand
    // side of a blurred, aspect-cropped copy of it. A cover that averages
    // bright but is dark where the text lands gets dark ink on a dark
    // backdrop -- about 1.2:1, i.e. invisible -- and no choice of ink value
    // fixes a wrong choice of ink. An outline does, by putting a hard edge
    // of the opposite ink around every glyph, so one of the two always
    // contrasts with whatever is actually behind it.
    //
    // Costs one node and one texture sample, not a pass: Text.Outline is
    // drawn by the distance-field glyph material itself, so it stays a
    // single batch of the same quads. The DropShadow/layer.enabled route
    // would allocate a render target per line and re-render it on every
    // frame of a scroll, which is the category of cost AlbumArt.qml and
    // Background.qml both document going out of their way to avoid.
    //
    // Alpha rather than the flat ink so it reads as a halo firming the
    // letters up, not as outlined lettering; over the common dark backdrop
    // it is close to invisible and only asserts itself where the artwork
    // comes up to meet the text.
    readonly property color colorTextHalo: Qt.rgba(0, 0, 0, 0.62)
    readonly property color colorTextHaloOnLight: Qt.rgba(1, 1, 1, 0.62)

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
