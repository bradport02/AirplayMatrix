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
