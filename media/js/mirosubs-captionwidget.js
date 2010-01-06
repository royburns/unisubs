goog.provide('mirosubs.CaptionWidget');

var $T = goog.bind(goog.dom.createTextNode, goog.dom);
var $SPAN = goog.bind(goog.dom.createDom, goog.dom, 'span', null);
var $INPUT = goog.bind(goog.dom.createDom, goog.dom, 'input', { type: "text"});
var $A = goog.bind(goog.dom.createDom, goog.dom, 'a', { href: "#"});
var $SPACE = function() { var span = $SPAN(); 
                          span.innerHTML = "&nbsp;"; 
                          return span; };
var $CLICK = function(elem, listener) {
    goog.events.listen(elem, goog.events.EventType.CLICK,
                       function(e) {
                           listener(e);
                           e.preventDefault();
                       });
};

mirosubs.CaptionWidget = function(uuid, video_id, username, save_captions_url) {
    var that = this;
    this.video_id = video_id;
    this.caption_div = goog.dom.$(uuid + "_captions");
    this.save_captions_url = save_captions_url;
    this.unitOfWork = new mirosubs.UnitOfWork(function() { that.workPerformed(); });
    this.saving = false;
    this.saveLink = goog.dom.$(uuid + "_saveLink");
    this.savingSpan = goog.dom.$(uuid + "_savingSpan");
    this.addSubtitlesLink = goog.dom.$(uuid + "_addSubtitles");
    $CLICK(this.saveLink, function(e) { that.saveClicked(e); });
    $CLICK(this.addSubtitlesLink, function(e) { that.addSubtitlesLinkClicked(e); });
};

mirosubs.CaptionWidget.wrap = function(identifier) {
    var uuid = identifier["uuid"];
    var video_id = identifier["video_id"];
    var username = identifier["username"];
    var save_captions_url = identifier["save_captions_url"];
    new mirosubs.CaptionWidget(uuid, video_id, username, save_captions_url);
};

mirosubs.CaptionWidget.prototype.addCaption = function(time, caption_text) {
    var caption = {};
    caption["id"] = new Date().getTime() + '';
    caption["time"] = time;
    caption["text"] = caption_text;
    this.unitOfWork.registerNew(caption);
};

mirosubs.CaptionWidget.prototype.workPerformed = function() {
    if (this.saving) 
        return;
    this.setSaveVisibility_(1);
};

mirosubs.CaptionWidget.prototype.saveClicked = function() {
    this.saving = true;
    this.setSaveVisibility_(2);
    var work = this.unitOfWork.getWork();
    this.unitOfWork.clear();

    var p = goog.json.parse;
    var s = goog.json.serialize;

    var args = {};
    args["video_id"] = this.video_id;
    args["updated"] = s(work.updated);
    args["inserted"] = s(work.neu);
    args["deleted"] = s(work.deleted);

    var that = this;
    goog.net.CrossDomainRpc.send(
        this.save_captions_url,
        function(event) {
            // TODO: check result in future.
            var result = p(p(event["target"]["responseText"])
                           ["result"]);
            that.saving = false;
            that.setSaveVisibility_(that.unitOfWork.containsWork() ? 1 : 0);
        },
        "POST",
        args);
};

mirosubs.CaptionWidget.prototype.setSaveVisibility_ = function(flags) {
    this.saveLink.style.display = 
        ((flags & 1) == 1) ? "" : "none";
    this.savingSpan.style.visibility = 
        ((flags & 2) == 2) ? "visible" : "hidden";
};

// see http://code.google.com/closure/compiler/docs/api-tutorial3.html#mixed
mirosubs["CaptionWidget"] = mirosubs.CaptionWidget;
mirosubs.CaptionWidget["wrap"] = mirosubs.CaptionWidget.wrap;

if (typeof(MiroSubsToEmbed) != 'undefined')
    for (var i = 0; i < MiroSubsToEmbed.length; i++)
        mirosubs.CaptionWidget.wrap(MiroSubsToEmbed[i]);
