import QtQuick
import QtQuick.Effects

// Large square artwork tile: rounded corners, lifted off the background
// with a soft drop shadow. The shadow is a separate blurred leaf Rectangle
// (not a shadowed copy of the tile itself) -- MultiEffect's hidden-source
// capture only reliably works for a source item with no children of its
// own (compare Background.qml's plain Image); a Rectangle+Image+Text tile
// rendered blank when used the same way, so the actual tile stays visible
// and normal, and only the shadow shape goes through the effect.
Item {
    id: root

    Rectangle {
        id: shadowShape
        anchors.fill: tile
        anchors.topMargin: 10 * Theme.uiScale
        radius: Theme.radiusArt
        color: Qt.rgba(0, 0, 0, 0.6)
        visible: false
    }

    MultiEffect {
        anchors.fill: shadowShape
        source: shadowShape
        blurEnabled: true
        blur: 1.0
        blurMax: 48 * Theme.uiScale
    }

    Rectangle {
        id: tile
        anchors.fill: parent
        radius: Theme.radiusArt
        color: Theme.colorSurface
        clip: true

        Image {
            anchors.fill: parent
            source: app.track.artworkSource
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            visible: app.track.artworkSource !== ""
        }

        Text {
            anchors.centerIn: parent
            visible: app.track.artworkSource === ""
            text: "♪"
            color: Theme.colorTextSecondary
            font.pixelSize: parent.width * 0.26
        }
    }
}
