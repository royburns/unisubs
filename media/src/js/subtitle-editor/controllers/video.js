// Amara, universalsubtitles.org
//
// Copyright (C) 2013 Participatory Culture Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see
// http://www.gnu.org/licenses/agpl-3.0.html.

(function() {

    var root = this;

    var VideoController = function($scope, SubtitleStorage) {
        /**
         * Responsible for initializing the video and all video controls.
         * @param $scope
         * @param SubtitleStorage
         * @constructor
         */

        // The Popcorn instance.
        //
        // If this is a YouTube video, force controls.

        var videoURLs = SubtitleStorage.getVideoURLs();

        $scope.pop = window.Popcorn.smart('#video', videoURLs);
        $scope.pop.controls(true);

        $scope.playChunk = function(start, duration) {
            // Play a specified amount of time in a video, beginning at 'start',
            // and then pause.

            // Pause the video, first.
            $scope.pop.play();

            // Remove any existing cues that may interfere.
            $scope.removeAllTrackEvents();

            if (start < 0) {
                start = 0;
            }

            // Set the new start time.
            $scope.pop.currentTime(start);

            // Set a new cue to pause the video at the end of the chunk.
            $scope.pop.cue(start + duration, function() {
                $scope.pop.pause();
            });

            // Play the video.
            $scope.pop.play();

        };
        $scope.removeAllTrackEvents = function() {

            var trackEvents = $scope.pop.getTrackEvents();
            for (var i = 0; i < trackEvents.length; i++) {
                $scope.pop.removeTrackEvent(trackEvents[i].id);
            }

        };
        $scope.togglePlay = function() {

            // If the video is paused, play it.
            if ($scope.pop.paused()) {
                $scope.pop.play();

            // Otherwise, pause it.
            } else {
                $scope.pop.pause();
            }

        };

        $scope.$root.$on('subtitle-key-up', function($event, options) {

            var parser = options.parser;
            var subtitle = options.subtitle;
            var value = options.value;

            // Update the Popcorn subtitle instance's text.
            $scope.pop.amarasubtitle(subtitle.$id, {
                text: parser.markdownToHTML(value)
            });

        });
        $scope.$root.$on('subtitle-ready', function($event, subtitle) {
            // When a subtitle is ready, we need to create a Popcorn subtitle bound to the
            // video's Popcorn instance.

            var parser = subtitle.parser;

            var text = subtitle.parser.markdownToHTML(AmarajQuery(subtitle.subtitle).text());
            var endTimeSeconds = parser.endTime(subtitle.subtitle) / 1000;
            var startTimeSeconds = parser.startTime(subtitle.subtitle) / 1000;

            // Create the amarasubtitle instance.
            $scope.pop.amarasubtitle(subtitle.$id, {
                end:   endTimeSeconds,
                start: startTimeSeconds,
                text:  text
            });

        });
        $scope.$root.$on('subtitle-selected', function($event, subtitle) {

            var parser = subtitle.parser;
            var startTimeSeconds = parser.startTime(subtitle.subtitle) / 1000;

            // If this video is not a Vimeo video, set the current time to
            // the start of the subtitle.
            //
            // We don't do this for Vimeo videos because their player doesn't support
            // fuzzy-scrubbing to precise keyframes.
            if ($scope.pop.video._util.type !== 'Vimeo') {
                $scope.pop.currentTime(startTimeSeconds);
            }

        });
    };

    root.VideoController = VideoController;

}).call(this);
