import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import QtQuick.Window

ApplicationWindow {
    id: window
    width: 960
    height: 600
    minimumWidth: 720
    minimumHeight: 480
    visible: true
    visibility: Window.FullScreen
    title: "AirPlay Desk Display"
    color: Theme.colorBackground

    Background {
        anchors.fill: parent
    }

    // Everything sizes off Theme.uiScale rather than fixed pixels, so the
    // layout holds together from the 720x480 minimum up through a maximised
    // or fullscreen window instead of looking tiny/cramped or oversized
    // relative to whatever's actually on screen.
    Binding {
        target: Theme
        property: "uiScale"
        value: Math.max(0.75, Math.min(2.5, Math.min(window.width / 960, window.height / 600)))
    }

    // Single position/lyrics poll shared by the lyrics panel and the
    // playback bar -- see NowPlayingView's docstring on why lyrics stay
    // pull-based rather than a second internal timer per consumer.
    QtObject {
        id: poller
        property real position: 0
        property real fraction: app.track.duration > 0 ? position / app.track.duration : 0
        property string previousLine: ""
        property string currentLine: ""
        property string nextLine: ""
    }

    Timer {
        interval: 250
        running: app.track.sessionActive
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            poller.position = app.track.currentPosition()
            // previous/next must land before currentLine: LyricsPanel's
            // transition reads root.nextLine the instant currentLine
            // changes, so nextLine has to already be fresh by then.
            poller.previousLine = app.lyrics.previousLineAt(poller.position)
            poller.nextLine = app.lyrics.nextLineAt(poller.position)
            poller.currentLine = app.lyrics.lineAt(poller.position)
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        StatusBar {
            Layout.fillWidth: true
            Layout.preferredHeight: implicitHeight
        }

        NowPlayingView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            previousLine: poller.previousLine
            currentLine: poller.currentLine
            nextLine: poller.nextLine
        }

        PlaybackBar {
            Layout.fillWidth: true
            Layout.preferredHeight: implicitHeight
            position: poller.position
            fraction: poller.fraction
        }
    }
}
