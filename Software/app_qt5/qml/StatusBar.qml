import QtQuick 2.15
import QtQuick.Layouts 1.15

// Just the clock. This used to also carry an AirPlay/matrix status pill
// pair for debugging connection state -- that's gone now that both sides
// are auto-detecting and reconnecting on their own; a viewer doesn't need
// to see the plumbing, only the clock stays as a small ambient touch.
Item {
    id: root
    implicitHeight: 44 * Theme.uiScale

    // Legibility strip for the clock over artwork/colour behind it -- same
    // reasoning as Background.qml's vignette, and off for the same reason:
    // idle is flat solid grey with nothing layered over it, clock included.
    Rectangle {
        anchors.fill: parent
        opacity: app.track.sessionActive ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durationSlow } }
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: Qt.rgba(0, 0, 0, 0.22) }
            GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0) }
        }
    }

    Timer {
        interval: 1000
        // root.visible tracks the sessionActive binding Main.qml puts on
        // this component -- no point waking up once a second to recompute
        // a clock string nothing is displaying on the idle/waiting screen.
        running: root.visible
        repeat: true
        triggeredOnStart: true
        onTriggered: clockText.text = Qt.formatDateTime(new Date(), "h:mm AP · ddd, MMM d")
    }

    Text {
        id: clockText
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.rightMargin: Theme.spacingLg
        color: Theme.colorTextSecondary
        font.family: Theme.fontFamily
        font.pixelSize: 13 * Theme.uiScale
        font.weight: Font.Medium
    }
}
