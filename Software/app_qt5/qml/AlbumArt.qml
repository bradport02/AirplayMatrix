import QtQuick 2.15
import QtGraphicalEffects 1.15

// Large square artwork tile: rounded corners, lifted off the background
// with a soft drop shadow. The shadow is a separate blurred leaf Rectangle
// (not a shadowed copy of the tile itself) -- ShaderEffectSource-based
// capture (what FastBlur/MultiEffect both wrap) only reliably works for a
// source item with no children of its own (compare Background.qml's plain
// Image); a Rectangle+Image+Text tile rendered blank when used the same
// way, so the actual tile stays visible and normal, and only the shadow
// shape goes through the effect.
//
// Qt5 note: this is QtGraphicalEffects' FastBlur standing in for Qt6's
// MultiEffect(blurEnabled: true) -- same hidden-leaf-source technique,
// `radius` in place of `blurMax`.
Item {
    id: root

    // Whether the artwork is done decoding, so the panel doesn't start
    // fading in around an image that isn't there yet -- on this hardware a
    // cover takes long enough to decode that the fade would otherwise be
    // most of the way through before the artwork appeared, which reads as
    // the artwork popping in rather than fading. Error and Null both count
    // as "ready": no artwork, or artwork that failed, is a final answer,
    // and waiting forever on one would leave the panel hidden for good.
    readonly property bool ready: artImage.ready

    // Decode size for the cover, latched so it only ever grows. It cannot
    // simply track the tile's width: changing sourceSize forces Qt to
    // decode the whole image again, and this panel is hidden
    // (visible: opacity > 0) for the length of every track-change fade --
    // during which a Layout is free to stop sizing its subtree and report
    // a width of 0. Following that down and back up meant two extra
    // full-size decodes per track change, landing exactly when the artwork
    // was supposed to be fading in, which is why it sometimes appeared
    // half-rendered or not at all. Latching upward settles on the real
    // tile size once and then never churns again.
    property int decodeSize: 256

    // What should be on screen right now: the incoming cover once a
    // transition has started, the published one otherwise. Used for
    // visibility as well as the image itself -- keying visibility off the
    // published source alone would keep the tile hidden through a
    // transition from a track that had no artwork to one that does.
    readonly property string effectiveSource:
        (app.track.readyToTransition && app.track.pendingArtworkSource)
        ? app.track.pendingArtworkSource
        : app.track.artworkSource
    onWidthChanged: if (width > decodeSize) decodeSize = Math.ceil(width)

    Rectangle {
        id: shadowShape
        anchors.fill: tile
        anchors.topMargin: 10 * Theme.uiScale
        radius: Theme.radiusArt
        color: Qt.rgba(0, 0, 0, 0.6)
        visible: false
    }

    FastBlur {
        anchors.fill: shadowShape
        source: shadowShape
        radius: 48 * Theme.uiScale
    }

    Rectangle {
        id: tile
        anchors.fill: parent
        radius: Theme.radiusArt
        color: Theme.colorSurface
        clip: true

        // sourceSize caps the *decode*, not the layout: without it Qt
        // decodes whatever the sender delivered (AirPlay artwork is
        // routinely 1400x1400 or larger) at full resolution and keeps the
        // whole thing as an uncompressed pixmap, just to draw it into a
        // tile a few hundred pixels wide. Pinning it to the tile's real
        // on-screen size turns that into a fraction of the decode work and
        // a fraction of the resident pixels, with nothing to see for it --
        // the extra detail was being thrown away at draw time anyway.
        // Crossfading pair rather than one Image: in "crossfade" transition
        // mode this is what dissolves one cover into the next. In "fade"
        // mode the swap still happens here, but the whole panel is faded
        // out around it, so it simply isn't seen -- which is why this needs
        // no mode switch of its own.
        CrossfadeImage {
            id: artImage
            anchors.fill: parent
            // Warmed as soon as the incoming cover arrives, then switched to
            // the moment the transition starts (readyToTransition) rather
            // than waiting for the snapshot publish. Because it's already
            // decoded by then, the dissolve begins in the same frame as the
            // text fade -- which is what makes the two read as one movement
            // instead of a stutter. Falls back to the published source
            // whenever there's no transition in flight, which is what keeps
            // the artwork correct for the rest of the track.
            preloadSource: app.track.pendingArtworkSource
            source: root.effectiveSource
            decodeSize: root.decodeSize
            duration: app.settings.transitionMode === "crossfade" ? app.settings.crossfadeMs : 0
            visible: root.effectiveSource !== ""
        }

        Text {
            anchors.centerIn: parent
            visible: root.effectiveSource === ""
            text: "♪"
            color: Theme.colorTextSecondary
            font.pixelSize: parent.width * 0.26
        }
    }
}
