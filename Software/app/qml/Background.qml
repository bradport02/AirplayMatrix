import QtQuick
import QtQuick.Effects

// Full-bleed backdrop. Idle: a flat, solid grey -- no gradient, nothing
// derived from artwork, because there's no track to derive anything from.
// Paused: the current artwork, blurred and darkened, standing in for the
// window chrome. Playing: a handful of soft colour blobs, one per artwork
// quadrant, fade in over that instead -- an abstraction of the artwork's
// colours rather than the artwork itself (a literal blurred cover was the
// previous attempt here and didn't read well).
Item {
    id: root

    readonly property bool cornersAvailable: app.track.cornerTopLeft !== ""
    readonly property color cTL: cornersAvailable ? app.track.cornerTopLeft : Theme.colorIdleBackground
    readonly property color cTR: cornersAvailable ? app.track.cornerTopRight : Theme.colorIdleBackground
    readonly property color cBL: cornersAvailable ? app.track.cornerBottomLeft : Theme.colorIdleBackground
    readonly property color cBR: cornersAvailable ? app.track.cornerBottomRight : Theme.colorIdleBackground

    // A single blurred, roughly circular patch of colour. Deliberately a
    // leaf item (no children of its own) as the MultiEffect source --
    // compare AlbumArt.qml's shadow shape -- a source item with children
    // renders blank here instead of blurring.
    component ColorBlob: Item {
        property alias color: fill.color
        Rectangle {
            id: fill
            anchors.fill: parent
            radius: width / 2
            visible: false
            Behavior on color { ColorAnimation { duration: 900 } }
        }
        MultiEffect {
            anchors.fill: fill
            source: fill
            blurEnabled: true
            blur: 1.0
            blurMax: 90 * Theme.uiScale
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

    MultiEffect {
        anchors.fill: parent
        source: artImage
        visible: app.track.artworkSource !== ""
        blurEnabled: true
        blur: 1.0
        blurMax: 64
        saturation: 0.4
        brightness: 0.05
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
