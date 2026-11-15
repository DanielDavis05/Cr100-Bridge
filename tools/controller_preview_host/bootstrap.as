// Native-host substitute for the stock SWF6 UI, from the offline setup proof of concept
// (research/cr100-emulator/ruffle). No device access; music.as adds the browse path.
_global.CR100Demo = {paired:false, registered:false, complete:false, language:"en-US", usage:null, history:[]};
_global.demoLater = function(target, method, delay) {
    var task = {target:target, method:method};
    task.run = function() { clearInterval(this.id); this.target[this.method](); };
    task.id = setInterval(task, "run", delay);
    task.cancel = function() { clearInterval(this.id); };
    return task;
};
_global.demoNoop = function() { return 0; };
_global.demoFalse = function() { return false; };
_global.demoTrue = function() { return true; };
_global.demoBus = function() {
    var bus = {listeners:[]};
    bus.addListener = function(listener) {
        for (var i=0; i<this.listeners.length; i++) if (this.listeners[i] == listener) return;
        this.listeners.push(listener);
    };
    bus.removeListener = function(listener) {
        for (var i=this.listeners.length-1; i>=0; i--) if (this.listeners[i] == listener) this.listeners.splice(i,1);
    };
    bus.emit = function(method, arg) {
        var copy = this.listeners.concat();
        for (var i=0; i<copy.length; i++) copy[i][method](arg);
    };
    return bus;
};
_global.RinconSystem = {
    IsDiagBuild:false, isProductRegistered:false, DefaultRegEmail:"demo@example.com",
    MaxRegistrationEmailLen:128, CountryCodeMenu:"United States:US\nCanada:CA\nUnited Kingdom:GB",
    getHouseholdId:function() { return ""; },
    getLanguageID:function() { return CR100Demo.language; },
    languageIsSet:demoTrue,
    saveLanguageID:function(id) { CR100Demo.language=id; return false; },
    restartedForLanguageChange:demoFalse,
    isTraceEnabled:demoFalse,
    assert:function(value) { if (!value) trace("CR100 ASSERT"); },
    assertFailed:function(value) { trace("CR100 ASSERT: " + value); },
    traceObject:demoNoop,
    fileExists:demoFalse,
    formatString:function(format) {
        var result=String(format);
        for (var i=1; i<arguments.length; i++) {
            result=result.split("%"+i+"$s").join(String(arguments[i]));
            result=result.split("%"+i+"$d").join(String(arguments[i]));
        }
        return result;
    },
    registerHHwithZP:function(udn) { CR100Demo.paired=true; return 0; },
    isValidEmailAddress:function(value) { return value.indexOf("@")>0 && value.indexOf(".")>0; },
    isValidPostalCode:function(country,value) { return value.length>0; },
    usageDataOptInUnset:function() { return CR100Demo.usage == null; },
    saveUsageDataOptIn:function(value) { CR100Demo.usage=value; },
    getLocalIPAddr:function() { return "192.0.2.100"; }
};
_global.RinconHWMon = {
    isDocked:true, regionIsUSCA:demoTrue, enableSleepTimer:demoNoop,
    setScrollWheelParams:demoNoop, resetStandbyCounter:demoNoop, click:demoNoop,
    addBatteryStateListener:demoNoop, removeBatteryStateListener:demoNoop,
    getSettingString:function() { return "On"; }
};
RinconSystem.addPowerManagementListener=demoNoop;
_global.Household=demoBus();
Household.AlarmClock={addRunAlarmListener:demoNoop};
Household.totalZonePlayers=0;
Household.customerID="";
Household.isRegistered=false;
Household.hasCompatibleInvisibleZone=false;
Household.getAssociatedZP=function() { return {isCompatible:true, UDN:"RINCON_DEMO", ZoneName:"Living Room"}; };
Household.searchForZonePlayers=function() {
    demoLater({run:function() { Household.emit("onZoneGroupsChanged"); }},"run",700);
};
_global.UpnpDeviceDiscovery=demoBus();
UpnpDeviceDiscovery.join=function() {
    this.emit("onDeviceEvent", {operation:"REGISTER_DEVICE", udn:"RINCON_DEMO"});
};
_global.RinconUpdateMgr=function() { this.setMusicReIndexComplete=demoNoop; };
_global.PostUpdateMgr=function() { this.doPostUpdateTasks=demoNoop; };
_global.CustomerRegistrationOp=function(zp,email,register,notify,country,postal) {
    this.requested=register === true;
    this.removeListener=demoNoop;
    this.runOnWorkerThread=function(listener) {
        this.listener=listener;
        demoLater(this,"finish",700);
    };
    this.finish=function() {
        CR100Demo.registered=this.requested;
        Household.isRegistered=this.requested;
        Household.customerID="OFFLINE-DEMO";
        RinconSystem.isProductRegistered=this.requested;
        this.listener.onUpnpOpDone(this,0);
    };
};
String.prototype.compareNoCase=function(other) {
    var a=this.toLowerCase(); var b=other.toLowerCase();
    return a<b ? -1 : (a>b ? 1 : 0);
};
String.prototype.addProperty("displayLength",function() { return this.length; },null);
// Sonos extended TextField with menu-line metrics and selection properties.
TextField.prototype.addProperty("textLineHeight", function() {
    var count=this.text.split("\r").join("\n").split("\n").length;
    return this.textHeight>0 ? this.textHeight/count : Number(this.getTextFormat().size)+3;
}, null);
TextField.prototype.addProperty("visibleLines", function() {
    return Math.max(1, Math.floor((this._height-4)/this.textLineHeight));
}, null);
TextField.prototype.addProperty("selectedLine", function() {
    return this.demoSelected == null ? 0 : this.demoSelected;
}, function(index) {
    this.demoSelected=Math.max(0,Math.min(index,this.text.split("\r").join("\n").split("\n").length-1));
});
TextField.prototype.getTextAtLine=function(index) { return this.text.split("\r").join("\n").split("\n")[index]; };
TextField.prototype.addListener=demoNoop;
TextField.prototype.removeListener=demoNoop;
// The original key listener uses ASCII device events; browser keys use keycodes.
_global.demoKeys={onKeyDown:function() {
    var code=Key.getCode();
    var name;
    if(code==13) name="OK";
    if(code==8 || code==27 || code==37) name="Back";
    if(code==39) name="OK";
    if(code==38) name="Up";
    if(code==40) name="Down";
    if(code==49) name="SoftButton1";
    if(code==50) name="SoftButton2";
    if(code==51) name="SoftButton3";
    if(name != null) { _root.HardwareKey(name,"down"); _root.HardwareKey(name,"up"); }
}};
Key.addListener(demoKeys);
