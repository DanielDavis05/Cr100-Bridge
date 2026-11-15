// Runs after the stock root scripts have installed their functions. The preview has
// no room model, so whenever the stock UI shows Rooms (once boot has settled, or after
// Back from the Music menu) it opens the Music (Browse) panel instead. Boot shows its
// "searching" popup about 700 ms in; opening earlier leaves that popup on top.
fscommand("cr100-ready","true");
_global.previewOpen={started:getTimer(), run:function() {
    if(getTimer()-this.started<1500 || g_PanelMgr.currentFrameLabel!="ZoneMenu") return;
    g_PanelMgr.navigateToPanel("Browse",{immediate:true});
    fscommand("cr100-state","Browse");
}};
previewOpen.id=setInterval(previewOpen,"run",200);
