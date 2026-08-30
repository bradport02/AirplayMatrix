import QtQuick 2.15
import QtGraphicalEffects 1.15

// Full-bleed backdrop. Idle: a flat, solid grey -- no gradient, nothing
// derived from artwork, because there's no track to derive anything from.
// Paused: the current artwork, blurred and darkened, standing in for the
// window chrome. Playing: a handful of soft colour blobs, one per artwork
// quadrant, fade in over that instead -- an abstraction of the artwork's
// colours rather than the artwork itself (a literal blurred cover was the
// previous attempt here and didn't read well).
//
// Qt5 note: this is a port of app/qml/Background.qml, which uses Qt6's
// MultiEffect for both the blur and a saturation/brightness colour-grade in
// one pass. QtGraphicalEffects (Qt5) has no single equivalent combining
// blur with colour grading, and stacking a second full-screen ShaderEffect
// purely for a 0.4 saturation / 0.05 brightness tweak is exactly the kind
// of extra per-frame GPU pass this port can't spend on unproven ARM1176
// hardware -- so that grading is dropped here and only the blur (FastBlur,
// in place of MultiEffect's blurEnabled/blurMax) is kept. The black scrim
// right below already does the legibility work the brightness tweak was
// for; losing the saturation dip is a minor, deliberate look difference,
// not a bug.
Item {
    id: root

    readonly property bool cornersAvailable: app.track.cornerTopLeft !== ""
    readonly property color cTL: cornersAvailable ? app.track.cornerTopLeft : Theme.colorIdleBackground
    readonly property color cTR: cornersAvailable ? app.track.cornerTopRight : Theme.colorIdleBackground
    readonly property color cBL: cornersAvailable ? app.track.cornerBottomLeft : Theme.colorIdleBackground
    readonly property color cBR: cornersAvailable ? app.track.cornerBottomRight : Theme.colorIdleBackground

    // A single blurred, roughly circular patch of colour. Deliberately a
    // leaf item (no children of its own) as the FastBlur source -- compare
    // AlbumArt.qml's shadow shape -- a source item with children renders
    // blank here instead of blurring.
    component ColorBlob: Item {
        property alias color: fill.color
        Rectangle {
            id: fill
            anchors.fill: parent
            radius: width / 2
            visible: false
            Behavior on color { ColorAnimation { duration: 900 } }
        }
        FastBlur {
            anchors.fill: fill
            source: fill
            radius: 90 * Theme.uiScale
        }
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.colorIdleBackground
    }

    Image {
        id: artImage
        anchors.fill: parent
        source: app.track.artworkSource
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: false
        visible: false
        opacity: 0

        onSourceChanged: opacity = 0
        onStatusChanged: if (status === Image.Ready) opacity = 1

        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow; easing.type: Easing.OutCubic } }
    }

    FastBlur {
        anchors.fill: parent
        source: artImage
        visible: app.track.artworkSource !== ""
        radius: 64 * Theme.uiScale
    }

    // Darkens the blurred artwork so foreground text stays legible, without
    // washing out its colour the way a heavier scrim would.
    Rectangle {
        anchors.fill: parent
        color: "black"
        opacity: app.track.artworkSource !== "" ? 0.35 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow } }
    }

    // Ambient colour wash -- only while actually playing (not merely an
    // open, paused session), so the blurred-artwork look above is what
    // shows the rest of the time. Four overlapping blobs, one per artwork
    // quadrant, pulled in from the true corners so they blend into each
    // other across the middle rather than leaving the centre empty.
    Item {
        id: colorWash
        anchors.fill: parent
        opacity: (app.track.playing && root.cornersAvailable) ? 1.0 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow; easing.type: Easing.OutCubic } }

        readonly property real blobSize: Math.max(width, height) * 0.85

        ColorBlob {
            color: root.cTL
            width: colorWash.blobSize; height: width
            x: colorWash.width * 0.22 - width / 2
            y: colorWash.height * 0.22 - height / 2
        }
        ColorBlob {
            color: root.cTR
            width: colorWash.blobSize; height: width
            x: colorWash.width * 0.78 - width / 2
            y: colorWash.height * 0.22 - height / 2
        }
        ColorBlob {
            color: root.cBL
            width: colorWash.blobSize; height: width
            x: colorWash.width * 0.22 - width / 2
            y: colorWash.height * 0.78 - height / 2
        }
        ColorBlob {
            color: root.cBR
            width: colorWash.blobSize; height: width
            x: colorWash.width * 0.78 - width / 2
            y: colorWash.height * 0.78 - height / 2
        }

        // Same dark-legibility scrim as the blurred-art layer, so text
        // contrast doesn't depend on how bright the sampled colours are.
        Rectangle {
            anchors.fill: parent
            color: "black"
            opacity: 0.3
        }
    }

    // Vignette so the top/bottom bars read clearly over artwork/colour --
    // only relevant once there's actually a session's worth of content
    // behind it. Idle is meant to be flat, solid grey with nothing layered
    // over it at all, so this stays fully off there rather than leaving a
    // faint light/dark/light banding across a background that's supposed
    // to read as one plain colour.
    Rectangle {
        anchors.fill: parent
        opacity: app.track.sessionActive ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow } }
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: Qt.rgba(0, 0, 0, 0.35) }
            GradientStop { position: 0.18; color: Qt.rgba(0, 0, 0, 0) }
            GradientStop { position: 0.82; color: Qt.rgba(0, 0, 0, 0) }
            GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.45) }
        }
    }
}
