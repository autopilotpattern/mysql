var hideLogSection = function(e) {
    e.parentNode.parentNode.style.display = "none";
}

var replaceTable = function(name) {
    return function(data) {
        thisDiv = document.getElementById(name);
        thisDiv.innerHTML = data;
    };
};

var appendTable = function(name) {
    return function(data) {
        thisDiv = document.getElementById(name);
        if (data.length > 0) {
            data = thisDiv.innerHTML + data;
            thisDiv.innerHTML = data.substr(data.length-1000); // truncate old
            thisDiv.scrollTop = thisDiv.scrollHeight;
            thisDiv.parentNode.style.display = "block";
        }
    };
};

var fillTable = function(fillFn, url) {
    var req = new XMLHttpRequest();
    req.onreadystatechange = function() {
        if (req.readyState == XMLHttpRequest.DONE) {
            var data = "";
            if (req.status === 200) {
                data = req.responseText;
            }
            fillFn(data);
        }
    }
    req.open('GET', url, true);
    req.send(null);
}

var scrollBottom = function(divName) {
    var thisDiv = document.getElementById(divName);
}

window.onload = function() {
    window.setInterval(function () {
        fillTable(replaceTable("dockerTable"), "docker");
        fillTable(replaceTable("consulTable"), "consul");
        fillTable(appendTable("mysql_1"), "mysql/1");
        fillTable(appendTable("mysql_2"), "mysql/2");
        fillTable(appendTable("mysql_3"), "mysql/3");
    }, 1000);
};
