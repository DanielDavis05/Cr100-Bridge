// Music layer on top of bootstrap.as: a household with one music service and the
// browse cache behind the Music menu, fed by the bridge's own SMAPI service.
// The web UI passes two FlashVars: smapiUrl (the same-origin preview route, which
// names the controller whose menu is shown) and serviceName (the menu title).
RinconSystem.getHouseholdId=function() { return "Sonos_PREVIEW"; };
RinconSystem.dealerLock=false; RinconSystem.storesAlive=false; RinconSystem.showContributing=false;
RinconSystem.setServiceErrorHandler=function(handler) { _global.PreviewServiceErrorHandler=handler; };
RinconSystem.removePowerManagementListener=demoNoop;
RinconSystem.FormatString=RinconSystem.formatString;
RinconSystem.resURIIsStreamingAudio=demoFalse; RinconSystem.resURIIsInternetRadio=demoFalse;
RinconHWMon.batteryBars=4; RinconHWMon.wifiStrengthBars=4;
RinconHWMon.addBatteryListener=demoNoop; RinconHWMon.removeBatteryListener=demoNoop;
RinconHWMon.addWifiStrengthListener=demoNoop; RinconHWMon.removeWifiStrengthListener=demoNoop;
Household.getNumZoneGroups=function() { return 0; };
Household.addZPListener=demoNoop; Household.removeZPListener=demoNoop;
Household.addMSListener=demoNoop; Household.removeMSListener=demoNoop;
_global.PreviewService={Title:(_level0.serviceName==null || _level0.serviceName=="") ? "Spotify" : _level0.serviceName,
    UDN:"SA_RINCON61447_bridge", hasMusic:true, isStubAccount:false,
    isDock:false, isPandora:false, isLastFM:false, canCacheTopLevelChildren:false};
PreviewService.canCacheContainer=demoFalse;
PreviewService.ContentProvider=PreviewService;
Household.numMediaServers=1;
Household.getMediaServerAt=function(ix) { return PreviewService; };
Household.lookupMediaServer=function(udn) { return udn==PreviewService.UDN ? PreviewService : null; };

// Rows come from SMAPI getMetadata, fetched when the stock UI primes a container and
// kept here for the synchronous BrowseCacheMgr reads: [title, id, upnp:class].
_global.PreviewCache={};
_global.PreviewPageSize=100;
_global.PreviewRowLimit=1000;
_global.previewRows=function(udn,id) {
    if(udn!=PreviewService.UDN) return null;
    var rows=PreviewCache[udn+"|"+id];
    return rows==null ? [] : rows;
};
_global.previewLocal=function(name) { var i=name.indexOf(":"); return i<0 ? name : name.substr(i+1); };
_global.previewFind=function(node,local) {
    if(node.nodeType==1 && previewLocal(node.nodeName)==local) return node;
    for(var c=node.firstChild;c!=null;c=c.nextSibling) {
        var hit=previewFind(c,local);
        if(hit!=null) return hit;
    }
    return null;
};
_global.previewText=function(node,local) {
    for(var c=node.firstChild;c!=null;c=c.nextSibling)
        if(c.nodeType==1 && previewLocal(c.nodeName)==local) return c.firstChild==null ? "" : c.firstChild.nodeValue;
    return null;
};
_global.previewEscape=function(value) {
    return String(value).split("&").join("&amp;").split("<").join("&lt;").split(">").join("&gt;");
};
_global.PreviewClasses={container:"object.container", search:"object.container", playlist:"object.container.playlistContainer",
    album:"object.container.album.musicAlbum", artist:"object.container.person.musicArtist",
    track:"object.item.audioItem.musicTrack", stream:"object.item.audioItem.audioBroadcast",
    program:"object.item.audioItem.audioBroadcast"};
_global.previewFault=function(response) {
    var fault=previewFind(response,"faultstring");
    return (fault==null || fault.firstChild==null) ? STR_GenericError : fault.firstChild.nodeValue;
};
// One getMetadata page. A SOAP fault (for example Client.FeatureLocked) goes to the stock
// service-error popup, as the native host reports provider faults; the bridge route
// returns faults with HTTP 200 because XML.sendAndLoad drops the body of other statuses.
_global.previewPage=function(smapiId,rows,done) {
    var request=new XML('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        +'<getMetadata xmlns="http://www.sonos.com/Services/1.1"><id>'+previewEscape(smapiId)+'</id>'
        +'<index>'+rows.length+'</index><count>'+PreviewPageSize+'</count></getMetadata></s:Body></s:Envelope>');
    request.contentType="text/xml; charset=utf-8";
    request.addRequestHeader("X-Bridge","1");
    var response=new XML();
    response.ignoreWhite=true;
    response.onLoad=function(ok) {
        var result=ok ? previewFind(this,"getMetadataResult") : null;
        if(result==null) {
            var message=ok ? previewFault(this) : STR_GenericError;
            fscommand("cr100-fault",smapiId+"\n"+message);
            if(PreviewServiceErrorHandler.onServiceError!=null) PreviewServiceErrorHandler.onServiceError(message);
            done();
            return;
        }
        var before=rows.length;
        for(var c=result.firstChild;c!=null;c=c.nextSibling) {
            var kind=previewLocal(c.nodeName);
            if(kind!="mediaCollection" && kind!="mediaMetadata") continue;
            var type=previewText(c,"itemType");
            var cls=PreviewClasses[type]==null ? "object.container" : PreviewClasses[type];
            rows.push([previewText(c,"title"),previewText(c,"id"),cls]);
        }
        var total=Number(previewText(result,"total"));
        if(rows.length>before && rows.length<total && rows.length<PreviewRowLimit) previewPage(smapiId,rows,done);
        else done();
    };
    request.sendAndLoad(_level0.smapiUrl,response);
};
_global.previewFetch=function(udn,id,done) {
    var smapiId=(id=="0") ? "root" : id;
    var rows=[];
    previewPage(smapiId,rows,function() {
        // Capabilities=1: the native host itself puts Search first in the service root.
        if(smapiId=="root") rows.unshift([STR_Search==null ? "Search" : STR_Search,"search","object.container"]);
        PreviewCache[udn+"|"+id]=rows;
        fscommand("cr100-rows",smapiId+"\n"+rows.length);
        done();
    });
};
_global.BrowseCacheMgr=demoBus();
BrowseCacheMgr.lastUpnpResult=0;
BrowseCacheMgr.addIndexListener=demoNoop; BrowseCacheMgr.removeIndexListener=demoNoop;
BrowseCacheMgr.stopPolling=demoNoop; BrowseCacheMgr.pollMS=demoNoop; BrowseCacheMgr.forcePoll=demoNoop;
BrowseCacheMgr.registerCP=demoNoop; BrowseCacheMgr.unregisterCP=demoNoop; BrowseCacheMgr.cancelCachePrime=demoNoop;
BrowseCacheMgr.invalidateContainer=demoNoop;
BrowseCacheMgr.primeCache=function(udn,id,index,handler) {
    previewFetch(udn,id,function() { handler.onPrimed(0); });
    return 0;
};
BrowseCacheMgr.getContainerTotalMatches=function(udn,id) {
    var rows=previewRows(udn,id);
    return rows==null ? 0 : rows.length;
};
BrowseCacheMgr.getChildProperty=function(udn,id,ix,prop) {
    var rows=previewRows(udn,id);
    return (rows==null || rows[ix]==null) ? null : rows[ix][prop];
};
BrowseCacheMgr.getNodeProperty=function(udn,id,prop) { return null; };
BrowseCacheMgr.getPropertyOfChildren=function(udn,id,first,count,prop,formatter) {
    var rows=previewRows(udn,id); var labels=[];
    if(rows!=null) for(var i=first;i<first+count && i<rows.length;i++) labels.push(rows[i][0]);
    return {labels:labels.join("\n"), labelCnt:labels.length};
};
BrowseCacheMgr.getSelectionFlagsForChild=function() { return 0; };

// Host class used by the Browse panel for its navigation stack (`new BrowseStack()`).
_global.BrowseStack=function() { this.length=0; };
BrowseStack.prototype.push=function(node) { this[this.length]=node; this.length++; return this.length; };
BrowseStack.prototype.pop=function() {
    if(this.length<=0) return undefined;
    this.length--;
    var node=this[this.length];
    delete this[this.length];
    return node;
};
BrowseStack.prototype.top=function() { return this.length>0 ? this[this.length-1] : undefined; };
BrowseStack.prototype.clear=function() { while(this.length>0) this.pop(); };

// Sonos's virtual-list TextField: setVL(count, scrollPos) announces the row count; the
// field then pulls rows through this.OnFetchData(first, n), which answers with setVLText.
// This version fetches every row at once and lets the ordinary text scroll do the rest.
TextField.prototype.setVL=function(count,scrollPos) {
    this.vlCount=count;
    this.vlLines=[];
    this.text="";
    if(count>0 && this.OnFetchData!=null) this.OnFetchData(0,count);
};
TextField.prototype.setVLText=function(labels,first,count) {
    var lines=String(labels).split("\r").join("\n").split("\n");
    if(this.vlLines==null) this.vlLines=[];
    for(var i=0;i<count;i++) this.vlLines[first+i]=lines[i];
    this.text=this.vlLines.join("\n");
};
