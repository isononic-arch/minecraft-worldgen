//-- get info on delimiter here: https://github.com/Captain-Chaos/WorldPainter/blob/219f7eb1402e49d9c79fed72799c82503385d669/WorldPainter/WPGUI/src/test/resources/descriptortest.js

// script.description=To specify where the rivers start make a temporary (custom ground cover) layer and\npaint SINGLE pixels on the map. Put the name of this layer in the "start position layer" field below.\n\nAnd/or specify how many random river origins the script should create.\n\nNOTE: There must be water on the map!\n\nType "help" in the parameters below for advanced settings.

// script.name= River Script -- by sijmen_v_b.

// script.param.startPositionLayer.type=string
// script.param.startPositionLayer.displayName=Name of start position layer  
// script.param.startPositionLayer.description=Name of the layer used to paint the starting positions.
// script.param.startPositionLayer.optional=true
// script.param.startPositionLayer.default=start

// script.param.randomStartingPositions.type=integer
// script.param.randomStartingPositions.description=The number of random starting positions to be placed on land.
// script.param.randomStartingPositions.displayName=Number of random start positions
// script.param.randomStartingPositions.default=0
// script.param.randomStartingPositions.optional=false

// script.param.startWidth.type=integer
// script.param.startWidth.description=The number of blocks the river is wide from the center line (radius) at the source of the river.
// script.param.startWidth.displayName=River width at the source  
// script.param.startWidth.default=3
// script.param.startWidth.optional=false

// script.param.endWidth.type=integer
// script.param.endWidth.description=The number of blocks the river is wide from the center line (radius) at the ocean.
// script.param.endWidth.displayName=River width at the ocean 
// script.param.endWidth.default=15
// script.param.endWidth.optional=false

// script.param.onlyFlowDown.type=boolean
// script.param.onlyFlowDown.description=Make sure the river only flows down by cutting into hills.
// script.param.onlyFlowDown.displayName=Rivers only flow down
// script.param.onlyFlowDown.default=true

// script.param.riverDepth.type=float
// script.param.riverDepth.description=The depth of the river as a proportion of the width of the river. (higher is deeper)
// script.param.riverDepth.displayName=River depth (relative to width)
// script.param.riverDepth.default=0.3499999940395355
// script.param.riverDepth.optional=false

// script.param.riverBraiding.type=boolean
// script.param.riverBraiding.description=When enabled rivers that join now twist around each other instead of joining.
// script.param.riverBraiding.displayName=River braiding
// script.param.riverBraiding.default=false

// script.param.random.type=integer
// script.param.random.description=Makes the rivers wiggle with higher values it starts wiggling on steeper terrain.
// script.param.random.displayName=Meander Strength (water escapes with high values)
// script.param.random.default=100
// script.param.random.optional=false

// script.param.noiseSize.type=integer
// script.param.noiseSize.description=The size of the wiggles higher values might need higher strength.
// script.param.noiseSize.displayName=Meander Size (in %)
// script.param.noiseSize.default=100
// script.param.noiseSize.optional=false

// script.param.lavaMode.type=boolean
// script.param.lavaMode.description=Replaces water with lava.
// script.param.lavaMode.displayName=Lava mode
// script.param.lavaMode.default=false

// script.param.minRiverLength.type=integer
// script.param.minRiverLength.description=Removes rivers shorter than this, especially helpful with random origins.
// script.param.minRiverLength.displayName=Minimum river length
// script.param.minRiverLength.default=100
// script.param.minRiverLength.optional=false

// script.param.avoidLayer.type=string
// script.param.avoidLayer.displayName=Name of layer to avoid
// script.param.avoidLayer.description=The path finding for the rivers wil not cross this layer.
// script.param.avoidLayer.optional=true
// script.param.avoidLayer.default=


//################################################################# \/ priority que \/ #################################################################

function Candidate(weight, x, y, dist) {
	this.weight = weight;
	this.x = x
	this.y = y
	this.dist = dist
}


Candidate.prototype.getWeight = function () {
	return this.weight;
}


Candidate.prototype.setWeight = function (value) {
	this.weight = value;
}


function PriorityQueue() {
	this.values = []
}

PriorityQueue.prototype.add = function (element) {
	this.values.push(element);
	var index = this.values.length - 1;
	var current = this.values[index];

	while (index > 0) {
		var parentIndex = Math.floor((index - 1) / 2);
		var parent = this.values[parentIndex];

		if (parent.weight >= current.weight) {
			this.values[parentIndex] = current;
			this.values[index] = parent;
			index = parentIndex;
		} else break;
	}
}

PriorityQueue.prototype.poll = function () {
	var max = this.values[0];
	var end = this.values.pop();
	this.values[0] = end;

	var index = 0;
	var length = this.values.length;
	var current = this.values[0];
	while (true) {
		var leftChildIndex = 2 * index + 1;
		var rightChildIndex = 2 * index + 2;
		var leftChild, rightChild;
		var swap = null;

		if (leftChildIndex < length) {
			leftChild = this.values[leftChildIndex];
			if (leftChild.weight < current.weight) swap = leftChildIndex;
		}
		if (rightChildIndex < length) {
			rightChild = this.values[rightChildIndex];
			if (
				(swap === null && rightChild.weight < current.weight) ||
				(swap !== null && rightChild.weight < leftChild.weight)
			) {
				swap = rightChildIndex;
			}
		}

		if (swap === null) break;
		this.values[index] = this.values[swap];
		this.values[swap] = current;
		index = swap;
	}

	return max;
}

//################################################################# /\ priority que /\ #################################################################

// ############# \/ save last entered value \/ ###############
var fs = Java.type('java.nio.file.Files');
var Paths = Java.type('java.nio.file.Paths');
var StandardOpenOption = Java.type('java.nio.file.StandardOpenOption');
var StandardCharsets = Java.type('java.nio.charset.StandardCharsets');
var System = Java.type('java.lang.System');
var scriptFilePath = scriptDir + "\\" + __FILE__
// ############# /\ save last entered value /\ ###############

var app = org.pepsoft.worldpainter.App.getInstance();
var d = new Date();
var startTime = d.getTime();
initRandom(this);
noise.seed(Math.random); // make this noise.seed(0); to make the noise consistent
var dimension = app.dimension;
var lookingAtTunnelLayer = false;
var report = ""; //stores all the information to be displayed 
var tunnelLayer = null
if (dimension.getAnchor().role == org.pepsoft.worldpainter.Dimension.Role.CAVE_FLOOR) {
	tunnelLayer = org.pepsoft.worldpainter.layers.tunnel.TunnelLayer.find(dimension);
	lookingAtTunnelLayer = true;
	print("Generating River in Tunnel Layer. (Make sure there is water for the river to end in!)")
	report += "Generating River in Tunnel Layer. (Make sure there is water for the river to end in!)\n"
}
var surfaceDimension = world.getDimension(dimension.getAnchor().dim);
var scale = 100;
extent = dimension.getExtent();
worldWidth = extent.getWidth() * 128;
worldHeight = extent.getHeight() * 128;
var minX = dimension.getLowestX() * 128;
var minY = dimension.getLowestY() * 128;
var thick = false;
var maskOn = 0;
var count = 0;
var numberOfRivers = 0;
var waterLevel = 62;
var runScript = true; //set to false to stop the script from running. used when issuing the help command.
var startPositionLayer = params['startPositionLayer'];
var randomStartingPositions = params['randomStartingPositions'];
var randomness = params['random'] * 10;
var noiseSize = params['noiseSize'];
var endWidth = params['endWidth'];
var startWidth = params['startWidth'];
var riverDepth = params['riverDepth'];
var onlyFlowDown = params['onlyFlowDown'];// determines if the river will only be allowed to go down and therefore
var lavaMode = params['lavaMode'];// determines if the river will only be allowed to go down and therefore
var minRiverLength = params['minRiverLength'];// removes rivers shorter than this.
var maxChangeSlope = 0.1; // the maximum allowed change in slope.
var MaxOrigins = 1000;// a limit on the starting positions that will be processed to prevent accidental masks that are not 1 pixel.
var lavaLayer = org.pepsoft.worldpainter.layers.FloodWithLava.INSTANCE
var HashMap = Java.type('java.util.HashMap');
var maskMap = new HashMap(); //stores [x,y,width_of_river]
var newWaterMap = new HashMap();//stores [x,y,[new_height1,...]]
var randomMap = new HashMap();// used to give the same random values each time.
//create map with all the terrain types. where the keys are the names lowercase without spaces. (use .replaceAll(" ","").toLocaleLowerCase())
var terrainMap = new HashMap();
var terrainEnum = org.pepsoft.worldpainter.Terrain.VALUES

for (var i = 0; i < terrainEnum.length; i++) {
	terrainMap.put(terrainEnum[i].toString().replaceAll(" ", "").toLocaleLowerCase(), terrainEnum[i]) // add the name to the and the terrain to the enum 
	terrainMap.put(terrainEnum[i].getName().replaceAll(" ", "").toLocaleLowerCase(), terrainEnum[i]) // also add the custom name so instead of custom4 you can also use the name of the custom layer.
}

var terrain; //used to store the terrain
var applyRiverTerrain = false; //do not change this value. will be done in the terrain section.  

var layerArr = [];

// load the layer from the GUI! (no longer require the layer to be on the map.) where the keys are the names lowercase without spaces. (use .replaceAll(" ","").toLocaleLowerCase())
var layers = app.getAllLayers();
var layerMap = new HashMap();

for (var i = 0; i < layers.length; i++) {
	layerMap.put(layers[i].getName().replaceAll(" ", "").toLocaleLowerCase(), layers[i])
}
var frostLayer = layerMap.get("Frost".replaceAll(" ", "").toLocaleLowerCase());
var BiomesLayer = org.pepsoft.worldpainter.layers.Biome.INSTANCE;
var frostRiverBiomeId = 11;
var frostBeachBiomeId = 26;
var riverBiomeId = 7;
var beachBiomeId = 16;
var oldNoise = false;// use the old math. random instead of simplex noise.
var riverBraiding = params['riverBraiding'];
var dykeSize = 0;//size of the dykes generated.
var avoidLayerName = params['avoidLayer'];
if (avoidLayerName == null) {
	avoidLayerName = "";
}
var avoidLayer = layerMap.get(avoidLayerName.replaceAll(" ", "").toLocaleLowerCase());
//var maskLayer = layerMap.get("mask".replaceAll(" ", "").toLocaleLowerCase());


// ############# \/ save last entered value \/ ###############
var paramDefaults = [
    ["// script.param.startPositionLayer.default=",startPositionLayer],
    ["// script.param.randomStartingPositions.default=",randomStartingPositions],
    ["// script.param.random.default=",randomness/10],
    ["// script.param.noiseSize.default=",noiseSize],
    ["// script.param.endWidth.default=",endWidth],
    ["// script.param.startWidth.default=",startWidth],
    ["// script.param.riverDepth.default=",riverDepth],
    ["// script.param.onlyFlowDown.default=",onlyFlowDown],
    ["// script.param.lavaMode.default=",lavaMode],
    ["// script.param.minRiverLength.default=",minRiverLength],
    ["// script.param.riverBraiding.default=",riverBraiding],
    ["// script.param.avoidLayer.default=",avoidLayerName]
]

var str = readFile(scriptFilePath);
newStr = replaceParamValue(str,paramDefaults)
createAndWriteFile(scriptFilePath, newStr);
// ############# /\ save last entered value /\ ###############

for (var i = 0; i < arguments.length; i++)//loop trough all arguments
{
	if (arguments[i].toLocaleLowerCase() == "terrain" || arguments[i].toLocaleLowerCase() == "t") {

		if (arguments.length < i + 1)//if there are not 2 lines following this keyword print an error.
		{
			print("ERROR! name of terrain was expected!");
			report += "ERROR! name of terrain was expected!\n";
		}
		else {
			terrain = terrainMap.get(arguments[i + 1].replaceAll(" ", "").toLocaleLowerCase())

			if (terrain != null) {
				applyRiverTerrain = true;
			} else {
				print("ERROR! can NOT find terrain with name \"" + arguments[i + 1] + "\" !")
				report += "ERROR! can NOT find terrain with name \"" + arguments[i + 1] + "\" !\n";
			}

			i++;
		}
	} else if (arguments[i].toLocaleLowerCase() == "layer" || arguments[i].toLocaleLowerCase() == "l") {

		if (arguments.length < i + 1)//if there are not 2 lines following this keyword print an error.
		{
			print("ERROR! name of layer was expected!");
			report += "ERROR! name of layer was expected!\n";
		}
		else {
			layer = layerMap.get(arguments[i + 1].replaceAll(" ", "").toLocaleLowerCase())

			if (layer != null) {
				layerArr.push(layer);
			} else {
				print("ERROR! cant find layer with name \"" + arguments[i + 1] + "\" !")
				report += "ERROR! cant find layer with name \"" + arguments[i + 1] + "\" !\n";
			}

			i++;
		}
	}
	else if (arguments[i].toLocaleUpperCase() == "MaxOrigins".toLocaleUpperCase() || arguments[i].toLocaleUpperCase() == "mo".toLocaleUpperCase()) {
		if (arguments.length < i + 1)//if there are not 1 lines following this keyword print an error.
		{
			print("!number of origins was expected!");
			report += "!number of origins was expected!\n";
		}
		else {
			MaxOrigins = arguments[i + 1];
			print("MaxOrigins has been set to " + MaxOrigins + ". (many origins can take a long time)");
			report += "MaxOrigins has been set to " + MaxOrigins + ". (many origins can take a long time)\n";
			i++;
		}
	}
	else if (arguments[i].toLocaleUpperCase() == "dykeSize".toLocaleUpperCase() || arguments[i].toLocaleUpperCase() == "d".toLocaleUpperCase()) {
		if (arguments.length < i + 1)//if there are not 1 lines following this keyword print an error.
		{
			print("! dykes size (0-100) was expected!");
			report += "! dykes size (0-100) was expected!\n";
		}
		else {
			dykeSize = arguments[i + 1] / 100;
			print("Dykes size has been set to " + dykeSize + ". (expected value: 0 - 100)");
			report += "Dykes size has been set to " + dykeSize + ". (expected value: 0 - 100)\n";
			i++;
		}
	}
	else if (arguments[i].toLocaleUpperCase() == "oldNoise".toLocaleUpperCase() || arguments[i].toLocaleUpperCase() == "on".toLocaleUpperCase()) {

		oldNoise = true;
		print("Old noise is now used!");
		report += "Old noise is now used!\n";


	}
	else if (arguments[i].toLocaleUpperCase() == "help".toLocaleUpperCase() || arguments[i].toLocaleUpperCase() == "h".toLocaleUpperCase()) {
		//print the help
		print("To set the terrain, use the following format:\n\nterrain\n[name of terrain]\n\nReplace [name of terrain] with the name of the terrain you want to use. You can only add one terrain, and it will affect the entire river.\n\nTo add a layer, use the following format:\n\nlayer\n[name of layer]\n\nReplace [name of layer] with the name of the layer you want to add. You can add multiple layers, and they will be applied on top of the river. where the strength of the layer will be highest in the middle.\n\nTo change the dyke/levee size, use the following format:\n\ndykeSize\n[number between 1-100]\n\nReplace [number between 1-100] by a number between 1-100\n\n\nYou can also shorten \"layer\" \"terrain\" and \"dykeSize\" to just their first letter.\n\n=================================== advanced: ===================================\n\nMaxOrigins\n[number of origins]\n\n\"MaxOrigins\" can be shortened to \"mo\". Max origins can be changed for big maps. This limit exists so you don't accidentally use the wrong layer and have to wait for 1000+ rivers to generate.\n\noldNoise\n\n\"oldNoise\" is used to go back to the old (frankly terrible) noise. this will disable the noise size and only look at the strength.\n\nIf you encounter any bugs contact me on discord: @sijmen_v_b .");
		runScript = false;
	}
	else if (arguments[i] == "") {
		//do nothing on empty line
	}
	else {
		print(arguments[i], " is not a valid command type \"help\" for help.");
		report += arguments[i] + " is not a valid command type \"help\" for help.\n";
	}
}


if (runScript) {

	var startPositions = [];

	if (startPositionLayer != null) {
		var originLayer = layerMap.get(startPositionLayer.replaceAll(" ", "").toLocaleLowerCase());
		if (originLayer == null) {
			print("ERROR! could NOT find layer with name \"" + startPositionLayer + "\"!")
			report += "ERROR! could NOT find layer with name \"" + startPositionLayer + "\"!\n";
		} else {

			var minTileX = dimension.getLowestX()
			var minTileY = dimension.getLowestY()
			for (var tileX = 0; tileX < extent.getWidth(); tileX++) {
				for (var tileY = 0; tileY < extent.getHeight(); tileY++) {
					//print("haslayer:", dimension.getTile(tileX + minTileX, tileY + minTileY).containsOneOf(originLayer))
					if (dimension.getTile(tileX + minTileX, tileY + minTileY) == null || !dimension.getTile(tileX + minTileX, tileY + minTileY).containsOneOf(originLayer)) {
						continue;
					}
					for (var x = tileX * 128; x < (tileX + 1) * 128; x++) {
						for (var y = tileY * 128; y < (tileY + 1) * 128; y++) {
							if (originLayer.getDataSize().toString() == "BIT") {
								if (dimension.getBitLayerValueAt(originLayer, x + minX, y + minY) && !avoidCondition(x + minX, y + minY)) {//add the start positions
									startPositions.push([x + minX, y + minY]);
								}
							} else {
								if (dimension.getLayerValueAt(originLayer, x + minX, y + minY) > 0 && !avoidCondition(x + minX, y + minY)) {//add the start positions
									startPositions.push([x + minX, y + minY]);
								}
							}
						}
					}
				}
				if (tileX % 10 == 0) {
					print("finding start positions: " + parseInt((tileX) / (extent.getWidth()) * 100 + 0.2) + "%");
				}
			}

		}
	}
	print("finding start positions: 100%");



	var maxTries = 50;//how often to retry when starting position is not on land.
	for (var i = 0; i < randomStartingPositions; i++) {
		var tries = 0;
		var randomX, randomY;
		do {
			randomX = Math.floor(Math.random() * worldWidth) + minX;
			randomY = Math.floor(Math.random() * worldHeight) + minY;
			tries++;
		} while (dimension.getHeightAt(randomX, randomY) < (dimension.getWaterLevelAt(randomX, randomY) - 0.5) && tries < maxTries && !avoidCondition(x + minX, y + minY));

		if (tries >= maxTries) {
			continue;
		}
		startPositions.push([randomX, randomY]);
	}

	if (startPositions.length == 0) {
		print("ERROR! No starting positions found!")
		report += "ERROR! No starting positions found!\n";
	}
	if (startPositions.length > MaxOrigins) {
		print("WARNING! More start positions found than the allowed maximum of " + MaxOrigins)
		print("This is to prevent the script taking too long if one accidentally paints more than single pixels. Use the \"MaxOrigins\" command to increase the maximum for big maps.")
		report += "WARNING! More start positions found than the allowed maximum of " + MaxOrigins + "\nThis is to prevent the script taking too long if one accidentally paints more than single pixels. Use the \"MaxOrigins\" command to increase the maximum for big maps.\n";
	}

	var foundPaths = []
	for (var i = 0; i < Math.min(startPositions.length, MaxOrigins); i++) {
		print("Finding path for river", i + 1, "of", Math.min(startPositions.length, MaxOrigins), "START POS:", startPositions[i])
		if (riverBraiding) {
			noise.seed(i);
		}
		path = findPath(startPositions[i][0], startPositions[i][1]);

		if (path.length < minRiverLength) {// if the rive ris shorter than the minimum length.
			//do not add the path
			print("This river is too short and won't be added.");
		} else if (dimension.getHeightAt(path[0][0], path[0][1]) > dimension.getHeightAt(path[path.length - 1][0], path[path.length - 1][1])) {
			//do not add the path
			print("The river starts lower than it ends and won't be added.");
		} else {
			foundPaths.push(path); //add the path
		}

	}


	numberOfRivers = foundPaths.length


	//make sure the terrain only flows down.
	if (onlyFlowDown) {
		print("making sure rivers only flow down")
		var stable = false;
		while (!stable) {//repeat until stable
			print("\trespecting gravity...");
			stable = true;//assume stable
			for (var pathIndex = 0; pathIndex < foundPaths.length; pathIndex++) {
				var path = foundPaths[pathIndex];
				var minHeight = 100000000000;
				for (var i = path.length - 1; i >= 0; i--) {
					var x = path[i][0];
					var y = path[i][1];
					if (x == null || y == null) {
						continue;
					}

					height = dimension.getHeightAt(x, y)
					if (height > minHeight) {
						dimension.setHeightAt(x, y, minHeight);
						stable = false;//if not stable repeat.
					} else {
						minHeight = height;
					}
				}
			}
		}
	}

	print("Calculating width, slope and depth.")
	for (var pathIndex = 0; pathIndex < foundPaths.length; pathIndex++) {
		var path = foundPaths[pathIndex];
		var minWaterDepth = dimension.getWaterLevelAt(path[0][0], path[0][1]); //get the water level at the end of the river.

		var minLength = 200;//the minimum apparent length. so shorter rivers will not have the full end width.
		var length = Math.max(path.length, minLength); // length of the river, taken to have a minimum length to prevent short rivers getting really thick.

		for (var i = path.length - 1; i >= 0; i--) {//go over the path starting from the origin.
			var x = path[i][0];
			var y = path[i][1];
			if (x == null || y == null) {
				continue;
			}

			var slope = 0
			if (i < path.length - 1) {
				slope = Math.min((dimension.getHeightAt(path[i + 1][0], path[i + 1][1]) - dimension.getHeightAt(x, y)), 1)
			}
			if (i > path.length - 5) {//set the slope higher for the start so it gets guardrails.
				slope = 0.5;
			}

			var width = parseInt((startWidth + ((length - (i - (path.length - length))) / length) * (endWidth - startWidth)) + 2 * (slope * slope)) // we use (i - Math.min(slope,2)*100) to make steep areas bigger to compensate for the space the guardrails take up.
			setMaskMaxSize(x, y, width, minWaterDepth, slope, 1)
		}
		print("Calculating width, slope and depth for river", pathIndex, "of", numberOfRivers);
		//extend the paths in the direction of the water to make the terrain blend better, make sure this does not break connections to tiny streams. (special only on water step?)
		var pointX = path[0][0];
		var pointY = path[0][1];

		var points = pathFindDown(pointX, pointY, endWidth * 4);

		for (var i = 0; i < points.length; i++) {

			var x = points[i][0];
			var y = points[i][1];

			var maxWidthReached = parseInt(startWidth + ((length - (0 - (path.length - length))) / length) * (endWidth - startWidth))

			setMaskMaxSize(x, y, parseInt(startWidth + (i / points.length) * (maxWidthReached - startWidth)), minWaterDepth, 0, i / points.length)
		}

	}

	print("Limiting the change of slope")
	//limit change of slope
	for (var pathIndex = 0; pathIndex < foundPaths.length; pathIndex++) {
		print("Limiting the change of slope for river", pathIndex, "of", numberOfRivers);
		var path = foundPaths[pathIndex];
		//print(path)
		if (path == null || path[0][0] == null || path[0][1] == null) {
			continue;
		}
		var previousHeight2 = dimension.getHeightAt(path[0][0], path[0][1]);
		var previousHeight1 = dimension.getHeightAt(path[1][0], path[1][1]);
		for (var i = 2; i < path.length; i++) {//go over the path starting from the ocean
			var x = path[i][0];
			var y = path[i][1];
			if (x == null || y == null) {
				continue;
			}

			var currHeight = dimension.getHeightAt(x, y)
			var prevSlope = Math.abs(previousHeight1 - previousHeight2);
			var currSlope = Math.abs(previousHeight1 - currHeight);
			//print(height + " previous: " + previousHeight)
			if (Math.abs(prevSlope - currSlope) > maxChangeSlope) {
				dimension.setHeightAt(x, y, Math.min(currHeight, previousHeight1 + (prevSlope + maxChangeSlope)));
			}
			previousHeight2 = previousHeight1;
			previousHeight1 = dimension.getHeightAt(x, y);

		}
	}





	//*
	var counter = 0;
	for (key in maskMap) { //for every block on a river (centre) path
		var arr = maskMap.get(key);
		var x = arr[0]
		var y = arr[1]
		var distance = arr[2]
		var minWaterDepth = arr[3]
		var slope = arr[4]
		var depthMultiplier = arr[5]

		loadTerranData(dimension, x, y, distance, minWaterDepth, slope, depthMultiplier);

		if (counter % 100 == 0) {
			print("loading the terrain data: " + parseInt((counter) / (maskMap.length) * 100 + 0.2) + "%");
		}
		counter++
	}


	print("Digging out the rivers and setting the terrain and layer(s)")
	var newWaterMapLength = newWaterMap.length
	counter = 0;
	//set the terrain.
	for (key in newWaterMap) {
		var arr = newWaterMap.get(key);
		var x = arr[0];
		var y = arr[1];
		var heightsArr = arr[2];
		var dist = arr[3];// between 0 and 1 where 0 is the center and 1 is the edge of the river.
		var widthArr = arr[4];
		var minWaterDepth = arr[5];
		var slope = arr[6];
		var depthMultiplier = arr[7];
		length = heightsArr.length
		if (length > 0) {
			var sum = 0;
			for (var i = 0; i < length; i++) {
				sum += heightsArr[i];
			}

			//calculate the width of the river
			widthSum = 0
			for (var i = 0; i < widthArr.length; i++) {
				widthSum += widthArr[i];
			}
			width = widthSum / widthArr.length;

			//set the water level:
			var newWaterLevel
			if (!(dist > 0.45 + 0.1 * (1 - clampBetweenZeroAndOne(slope * 8)))) { //if there is a slope and therefore river "guardrails" do not set the water near the edge.
				//slope correction, used to lower the water on steep and broad rivers.
				slopeCorrectionStartWidth = 7; //river width at witch it starts to lower the water.
				slopeCorrectionFallOff = 4; //the slope of the falloff the width will be divided by this.
				slopeCorrection = clampBetweenZeroAndOne((width - slopeCorrectionStartWidth) / slopeCorrectionFallOff) * Math.max(clampBetweenZeroAndOne(slope * 4), 0.25);

				newWaterLevel = Math.max((sum / length) - 0.5 - 1.3 * clampBetweenZeroAndOne(2 * slopeCorrection), minWaterDepth);

				dimension.setWaterLevelAt(x, y, newWaterLevel);
			}
			//set the terrain.
			var factor = 0.0;

			factor = Math.min(dist, 0.99);



			var depth = (width * riverDepth + 1.5) * depthMultiplier;
			var riverGuardRailsScale = 4.2 * Math.max(0.32 + dykeSize //the minimum dyke size.
				, Math.min(1.2, slope)); //the steepness of the terrain.

			var newHeight = (1 - factor) * ((sum / length) - depth + factor * (depth * riverGuardRailsScale)) + factor * dimension.getHeightAt(x, y)
			dimension.setHeightAt(x, y, newHeight);



			//add the terrain to the river.
			if (applyRiverTerrain) {
				if ((((dist < 0.4) && depthMultiplier == 1) || (Math.random() * 0.5 + 0.5) * depthMultiplier > dist + (0.25 * (1 - (width / endWidth) * (width / endWidth)))) && newHeight - newWaterLevel < 1.5) {
					dimension.setTerrainAt(x, y, terrain)
				}
			}


			//add layer(s) on river.
			//add lava option.
			if (lavaMode && dimension.getWaterLevelAt(x, y) > minWaterDepth) { //if the new water is not at the same heigh as the end river/ocean.
				dimension.setBitLayerValueAt(lavaLayer, x, y, true);
			}

			for (var i = 0; i < layerArr.length; i++) {
				layer = layerArr[i];

				if (layer.getDataSize().toString() == "BIT") {
					if (dimension.getBitLayerValueAt)
						dimension.setBitLayerValueAt(layer, x, y, 1)
				} else {
					dimension.setLayerValueAt(layer, x, y, parseInt((1 - dist) * 8) % 16)
				}

			}

			//add Biomes.
			if (dimension.getWaterLevelAt(x, y) > dimension.getHeightAt(x, y)) {
				if (dimension.getBitLayerValueAt(frostLayer, x, y)) {
					dimension.setLayerValueAt(BiomesLayer, x, y, frostRiverBiomeId)
				} else {
					dimension.setLayerValueAt(BiomesLayer, x, y, riverBiomeId)
				}
			} else {
				if (dimension.getBitLayerValueAt(frostLayer, x, y)) {
					dimension.setLayerValueAt(BiomesLayer, x, y, frostBeachBiomeId)
				} else {
					dimension.setLayerValueAt(BiomesLayer, x, y, beachBiomeId)
				}
			}

		}

		//print progress
		if (counter % 1000 == 0) {
			print("Digging out the rivers: " + parseInt((counter) / (newWaterMapLength) * 100 + 0.2) + "%");
		}
		counter++
	}
	//*/
	//fix water escaping:
	print("making sure water does not escape the river.")
	counter = 0;
	//set the terrain.
	for (key in newWaterMap) {
		var adjacentPoints = [
			{ x: 0, y: 1 },
			{ x: 0, y: -1 },
			{ x: 1, y: 0 },
			{ x: -1, y: 0 }
		];

		var sparseCirclePoints = [
			{ x: 3, y: 0 },
			{ x: 2, y: 2 },
			{ x: 0, y: 3 },
			{ x: 2, y: -2 },
			{ x: 0, y: -3 },
			{ x: -2, y: -2 },
			{ x: -3, y: 0 },
			{ x: -2, y: 2 },
		];
		var arr = newWaterMap.get(key);
		var x = arr[0];
		var y = arr[1];
		var dist = arr[3];// between 0 and 1 where 0 is the center and 1 is the edge of the river.
		var minWaterDepth = arr[5];
		var slope = arr[6];
		length = heightsArr.length
		if (length > 0) {
			if ((dist > 0.45 + 0.1 * (1 - clampBetweenZeroAndOne(slope * 8)))) {
				currentHeight = parseInt(dimension.getHeightAt(x, y) - 0.5);
				for (var k = 0; k < adjacentPoints.length; k++) {
					var point = adjacentPoints[k];
					var i = point.x;
					var j = point.y;
					currentHeight = parseInt(dimension.getHeightAt(x, y) - 0.5);
					if (parseInt(dimension.getWaterLevelAt(x + i, y + j) - 0.5) >= currentHeight && dimension.getWaterLevelAt(x + i, y + j) != minWaterDepth) {

						var maxWaterLevel = dimension.getWaterLevelAt(x + i, y + j)//+0.5
						//get water level:
						for (var l = 0; l < sparseCirclePoints.length; l++) {
							var sparseCirclePoint = sparseCirclePoints[l];
							var ix = sparseCirclePoint.x;
							var jy = sparseCirclePoint.y;
							maxWaterLevel = Math.max(maxWaterLevel, dimension.getWaterLevelAt(x + ix, y + jy))
						}

						dimension.setHeightAt(x, y, maxWaterLevel)
						//dimension.setBitLayerValueAt(maskLayer, x, y, true);
					}
				}
			}
		}

		//print progress
		if (counter % 1000 == 0) {
			print("restricting water to the bounds of the river: " + parseInt((counter) / (newWaterMapLength) * 100 + 0.2) + "%");
		}
		counter++
	}



	//run fixify on the rivers

	print("Running fixify on the rivers")
	counter = 0;
	for (key in newWaterMap) {
		var arr = newWaterMap.get(key);
		var x = arr[0];
		var y = arr[1];
		var dist = arr[3];
		if (x - minX < 1 || x - minX > worldWidth || y - minY < 1 || y - minY > worldHeight) {//check if coordinatres are within the map
			continue;//skip to next iteration of for loop
		}

		fixupRelaxed(dimension, x, y);

		if (counter % 1000 == 0) {
			print("Removing one by one block holes: " + parseInt((counter) / (newWaterMapLength) * 100 + 0.2) + "%");
		}
		counter++
	}



	print("fixing water on narrow river sections")
	for (var pathIndex = 0; pathIndex < foundPaths.length; pathIndex++) {
		print("fixing water on narrow river section", pathIndex, "of", numberOfRivers);
		var path = foundPaths[pathIndex];
		if (path == null || path[0][0] == null || path[0][1] == null) {
			continue;
		}
		for (var i = 0; i < path.length; i++) {
			var x = path[i][0];
			var y = path[i][1];
			if (parseInt(dimension.getHeightAt(x, y) - 0.5) >= (dimension.getWaterLevelAt(x, y) - 1)) {
				//dimension.setBitLayerValueAt(maskLayer, x, y, true);
				dimension.setHeightAt(x, y, dimension.getWaterLevelAt(x, y) - 1);
			}
		}
	}




	print("\n\n=============  Report:  =============\n" + report);


	d = new Date();
	var endTime = d.getTime();
	var elapsedMs = endTime - startTime;

	// Convert elapsed time from milliseconds to seconds
	var elapsedSec = Math.floor(elapsedMs / 1000);

	// Calculate hours, minutes, and seconds
	var hours = Math.floor(elapsedSec / 3600);
	var minutes = Math.floor((elapsedSec - (hours * 3600)) / 60);
	var seconds = elapsedSec - (hours * 3600) - (minutes * 60);
	var milliseconds = elapsedMs - (elapsedSec * 1000);

	// Format the time string
	var timeStr = '';
	if (hours > 0) {
		timeStr += hours + ' hour';
		if (hours > 1) {
			timeStr += 's';
		}
		timeStr += ' ';
	}
	if (minutes > 0 || hours > 0) {
		timeStr += minutes + ' minute';
		if (minutes > 1) {
			timeStr += 's';
		}
		timeStr += ' ';
	}
	if (seconds > 0 || minutes > 0 || hours > 0) {
		timeStr += seconds + ' second';
		if (seconds > 1) {
			timeStr += 's';
		}
		timeStr += ' ';
	}
	timeStr += milliseconds + ' millisecond';
	if (milliseconds != 1) {
		timeStr += 's';
	}
	print("took:\t" + timeStr);
}

print("\nDone! generated " + numberOfRivers + " rivers -- script provided by sijmen_v_b");



function repeatableRandom(x, y) {
	if (oldNoise) {
		key = toCoordinate(x, y);
		value = randomMap.get(key);
		if (value == null) {
			value = Math.random();
			randomMap.put(key, value);
		}
		return value;
	}

	x = x * 100 / noiseSize
	y = y * 100 / noiseSize

	noiseStrength = 30;
	DisplacementSize = 30;

	result = reMap(Math.abs(
		Math.sin((x + 200 * noise.simplex2((x + 1000) / 1000, (y + 1000) / 1000) + noiseStrength * noise.simplex2(x / (2000 / DisplacementSize), y / (2000 / DisplacementSize))) / DisplacementSize)
		+ Math.sin((y + noiseStrength * noise.simplex2((x + 100) / (2000 / DisplacementSize), (y + 100) / (2000 / DisplacementSize))) / DisplacementSize)
	), 0, 1, 0, 1)
	result = Math.max(result, 0.02)
	return reMap(result, 0, 2, 0, 1);

}

function reMap(num, fromMin, fromMax, toMin, toMax) {
	return ((num - fromMin) / (fromMax - fromMin)) * (toMax - toMin) + toMin
}

function loadTerranData(dimension, x, y, distance, minWaterDepth, slope, depthMultiplier) {
	var height = dimension.getHeightAt(x, y)
	for (var i = -1 * distance; i < distance + 1; i++) {
		for (var j = -1 * distance; j < distance + 1; j++)//loop trough all coordinates(blocks) of the map.
		{
			if (x + i > minX && x + i < worldWidth + minX && y + j > minY && y + j < worldHeight + minY) {
				var dist = getSquaredDistance(x + i, y + j, x, y);
				if (dist <= distance * distance) {

					//var strength = dist / distance * distance;
					addWater(x + i - minX, y + j - minY, height, dist / (distance * distance), distance, minWaterDepth, slope, depthMultiplier);
				}
			}
		}
	}
}

// updates maskMap to include a [x,y,size] array where if the coordinate is already exists it makes size the maximum of the two.
function setMaskMaxSize(x, y, size, minWaterDepth, slope, depthMultiplier) {
	key = toCoordinate(x, y);
	current = maskMap.get(key);
	if (current == null || current[2] < size) {
		maskMap.put(key, [x, y, size, minWaterDepth, slope, depthMultiplier])
	}
}

// appends the height to the map of blocks around the river. also passes information on the distance from the centre arr[3] and the width at that point (as a list) arr[4] 
function addWater(x, y, height, distanceFromCenter, width, minWaterDepth, slope, depthMultiplier) {
	key = toCoordinate(x + minX, y + minY);
	arr = newWaterMap.get(key);
	if (arr == null) {
		arr = [x + minX, y + minY, [], distanceFromCenter, [], minWaterDepth, slope, depthMultiplier];
	}
	arr[2].push(height);
	current = arr[3];
	if (current > distanceFromCenter) {
		arr[3] = distanceFromCenter;
	}
	arr[4].push(width);

	current = arr[5];
	if (current > minWaterDepth) {
		arr[5] = minWaterDepth;
	}

	current = arr[6];
	if (current > slope) {
		arr[6] = slope;
	}

	current = arr[7];
	if (current > depthMultiplier) {
		arr[7] = depthMultiplier;
	}
	newWaterMap.put(key, arr);
}

function getSquaredDistance(x1, y1, x2, y2) {
	var x = x1 - x2;
	var y = y1 - y2;
	return x * x + y * y;
}

function contains(coordList, coord) {
	for (var i = 0; i < coordList.length; i++) {
		if (coordList[i][0] === coord[0] && coordList[i][1] === coord[1]) {
			return true;
		}
	}
	return false;
}
//used for the hashmap
function toCoordinate(x, y) {
	return x + y * worldWidth
}

//take an x and a y and the hashmap storing arrays (with length 2) of x and y  to trace back to the start
function getTrail(x, y, coordinates) {
	var trail = [];
	while (x != null && y != null) {
		cameFrom = coordinates.get(toCoordinate(x, y))
		trail.push([x, y]);
		x = cameFrom[0];
		y = cameFrom[1];
	}
	return trail;
}

//gives me the distance of all the 8 directions of x and y where x and y are either 1 or 0 (so for a 3x3 grid)
function calculateCelDist(x, y) {
	if (Math.abs(x) + Math.abs(y) <= 1) {
		return 1.0;

	} else {
		return Math.sqrt(x * x + y * y);
	}
}

//used for path finding in water
function calculateCelDist2(x, y) {
	if (Math.abs(x) + Math.abs(y) <= 1) {
		return 1.0;
	} else {
		return 1.4142135623730951;
	}
}

//gives the intermediary blocks in case we use the "rim of 5x5 square without the corners".
//returns a list of tuples.
function getIntermediatePositions(x, y) {
	if (Math.max(Math.abs(x), Math.abs(y)) < 2) {
		return []
	}

	if (Math.abs(x) == 2) {
		if (y == 0) {
			return [{ x: x - x / 2, y: 0 }]
		}
		return [{ x: x - x / 2, y: 0 }, { x: x - x / 2, y: y }]
	} else { //y is biggest component
		if (x == 0) {
			return [{ x: 0, y: y - y / 2 }]
		}
		return [{ x: 0, y: y - y / 2 }, { x: x, y: y - y / 2 }]
	}
}

//function that returns true if the river may not cross this point.
function avoidCondition(x, y) {
	if (avoidLayer == null) {
		return false;
	}
	if (avoidLayer.getDataSize().toString() == "BIT") {
		if (dimension.getBitLayerValueAt(avoidLayer, x, y)) {//add the start positions
			return true;
		}
	} else {
		if (dimension.getLayerValueAt(avoidLayer, x, y) > 0) {//add the start positions
			return true;
		}
	}

	return false;
}

function findPath(x, y) {
	/* var points = [
		{ x: -1, y: -1 },
		{ x: -1, y: 0 },
		{ x: -1, y: 1 },
		{ x: 0, y: -1 },
		{ x: 0, y: 1 },
		{ x: 1, y: -1 },
		{ x: 1, y: 0 },
		{ x: 1, y: 1 },
	]; */
	// rim of 5x5 square without the corners.
	var points = [
		{ x: 2, y: 1 },
		{ x: 2, y: 0 },
		{ x: 2, y: -1 },
		{ x: -2, y: 1 },
		{ x: -2, y: 0 },
		{ x: -2, y: -1 },
		{ x: 1, y: 2 },
		{ x: 0, y: 2 },
		{ x: -1, y: 2 },
		{ x: 1, y: -2 },
		{ x: 0, y: -2 },
		{ x: -1, y: -2 }
	];

	if (x - minX < 0 || x - minX >= worldWidth || y - minY < 0 || y - minY >= worldHeight) {//check if coordinatres are within the map
		print("ERROR! starting position not on the map!");
		report += "ERROR! starting position not on the map!\n";
		return [[null, null]];
	}

	if (dimension.getHeightAt(x, y) < (dimension.getWaterLevelAt(x, y) - 0.5)) {
		print("Warning! starting position already on water, skipping (" + x + "," + y + ")!");
		report += "Warning! starting position already on water, skipping (" + x + "," + y + ")!\n";
		return [[null, null]];
	}

	openSet = new PriorityQueue(); //contains a array with [priority,x,y,current length from start]
	openSet.add(new Candidate(4, x, y, 4));

	var addedPositions = new HashMap(); //contains the nodes that where added and where they came from
	addedPositions.put(toCoordinate(x, y), [null, null])

	var previousBool = false; //used to check if the pathfinding is stuck
	var count = 0
	while (openSet.values.length != 0 && addedPositions.length < worldWidth * worldHeight) {//go over all the candidates till we have no more or we looked at every pixel of the map.

		current = openSet.poll();
		if (count % 100000 == 99999) {
			print("pathfinding info: number of candidates = " + openSet.values.length + " addedPositions = " + addedPositions.length + "  max: " + worldWidth * worldHeight)

			//check if the pathfinding is stuck now and if it was stuck last time.
			if (openSet.values.length == 1) {
				if (previousBool) {
					print("Warning! could not find any path towards water!, skipping (" + x + "," + y + ")!");
					report += "Warning! could not find any path towards water!, skipping (" + x + "," + y + ")!\n";
					return [[null, null]];
				} else {
					previousBool = true;
				}
			}
			else {
				previousBool = false;
			}
		}



		count++

		if (dimension.getHeightAt(current.x, current.y) < (dimension.getWaterLevelAt(current.x, current.y) - 0.5)) {
			return getTrail(current.x, current.y, addedPositions);
		}
		//add adjacent

		// shuffle the points so they are added in a random order:
		// shuffle the array using the Fisher-Yates algorithm
		for (var i = points.length - 1; i > 0; i--) {
			var j = Math.floor(Math.random() * (i + 1));
			var temp = points[i];
			points[i] = points[j];
			points[j] = temp;
		}


		for (var k = 0; k < points.length; k++) {
			var point = points[k];
			var i = point.x;
			var j = point.y;
			if (current.x + i - minX < 0 || current.x + i - minX >= worldWidth || current.y + j - minY < 0 || current.y + j - minY >= worldHeight//check if coordinates are within the map
				|| avoidCondition(current.x + i, current.y + j)
				|| (lookingAtTunnelLayer && !(lookingAtTunnelLayer && surfaceDimension.getBitLayerValueAt(tunnelLayer, current.x + i, current.y + j)))) //check if the height exists (used for custom cave floor dimension)
			{
				continue
			}
			if (addedPositions.get(toCoordinate(current.x + i, current.y + j)) == null) {//if not already reached
				//calculate the weight
				var distanceSaveBeforeRerouting = 150;//150; //the number of blocks the river must be shorter in order to go up 1 block. 
				var weight = (current.dist + calculateCelDist(i, j)) * 1 + (distanceSaveBeforeRerouting * dimension.getHeightAt(current.x + i, current.y + j)) + (repeatableRandom(current.x, current.y) * randomness)
				//add it to the que
				//make sure we add the intermediate coordinates.
				var candidates = getIntermediatePositions(i, j);
				if (candidates == null) {
					addedPositions.put(toCoordinate(current.x + i, current.y + j), [current.x, current.y])
				} else {
					var bestX = candidates[0].x;
					var bestY = candidates[0].y;
					var minHeight = Infinity
					if (addedPositions.get(toCoordinate(current.x + bestX, current.y + bestY)) == null) {
						minHeight = dimension.getHeightAt(current.x + bestX, current.y + bestY)
					}
					for (var candidateIndex = 1; candidateIndex < candidates.length; candidateIndex++) {
						var newX = candidates[candidateIndex].x;
						var newY = candidates[candidateIndex].y;
						var newHeight = dimension.getHeightAt(current.x + newX, current.y + newY)
						if (newHeight < minHeight && addedPositions.get(toCoordinate(current.x + newX, current.y + newY)) == null) {
							minHeight = newHeight;
							bestX = newX;
							bestY = newY;

						}
					}
					if (minHeight != Infinity) { //if the intermediary was not already added.
						addedPositions.put(toCoordinate(current.x + bestX, current.y + bestY), [current.x, current.y]);
						//dimension.setBitLayerValueAt(maskLayer, current.x + bestX, current.y + bestY, true)
					}
					addedPositions.put(toCoordinate(current.x + i, current.y + j), [current.x + bestX, current.y + bestY]);
					//dimension.setBitLayerValueAt(maskLayer, current.x + i, current.y + j, true)

					//addedPositions.put(toCoordinate(current.x + i, current.y + j), [current.x, current.y]);
				}

				openSet.add(new Candidate(weight, current.x + i, current.y + j, current.dist + calculateCelDist(i, j)))


			}
		}

	}

	var d = openSet.poll()
	return [[d.x, d.y]];
}


//similar to findPath but instead of stopping at water it will stop as soon as a path with length(actual distance not number of blocks.) of distanceTarget is reached.
function pathFindDown(x, y, distanceTarget) {

	if (x - minX < 0 || x - minX >= worldWidth || y - minY < 0 || y - minY >= worldHeight || avoidCondition(x, y)) {//check if coordinates are within the map
		print("ERROR! starting position not on the map!");
		report += "ERROR! starting position not on the map!\n";
		return [[null, null]];
	}

	var openSet = new PriorityQueue(); //contains a array with [priority,x,y,current length from start]
	openSet.add(new Candidate(4, x, y, 0));

	var addedPositions = new HashMap(); //contains the nodes that where added and where they came from
	addedPositions.put(toCoordinate(x, y), [null, null])


	var count = 0
	while (openSet.values.length != 0 && addedPositions.length < worldWidth * worldHeight) {//go over all the candidates till we have no more or we looked at every pixel of the map.

		current = openSet.poll();
		if (count % 10000 == -1 % 10000) {
			print("FIXING RIVER TO SEA! number of candidates = " + openSet.values.length + " addedPositions = " + addedPositions.length + "  max:" + worldWidth * worldHeight)
		}
		count++

		if (current.dist > distanceTarget) {
			return getTrail(current.x, current.y, addedPositions);
		}
		//add adjacent
		for (var i = -1; i < 2; i++) {
			for (var j = -1; j < 2; j++) {
				if (i == 0 && j == 0) {
					continue
				}
				if (current.x + i - minX < 0 || current.x + i - minX >= worldWidth || current.y + j - minY < 0 || current.y + j - minY >= worldHeight //check if coordinates are within the map
					|| avoidCondition(current.x + i, current.y + j)
					|| (lookingAtTunnelLayer && !(lookingAtTunnelLayer && surfaceDimension.getBitLayerValueAt(tunnelLayer, current.x + i, current.y + j)))) //check if the height exists (used for custom cave floor dimension)
				{
					continue
				}
				if (addedPositions.get(toCoordinate(current.x + i, current.y + j)) == null) {//if not already reached
					//calculate the weight
					var distanceSaveBeforeRerouting = 150; //the number of blocks the river must be shorter in order to go up 1 block. 
					weight = current.dist + calculateCelDist2(i, j) + distanceSaveBeforeRerouting * dimension.getHeightAt(current.x + i, current.y + j)
					//add it to the que
					addedPositions.put(toCoordinate(current.x + i, current.y + j), [current.x, current.y])
					openSet.add(new Candidate(weight, current.x + i, current.y + j, current.dist + calculateCelDist2(i, j)))
				}
			}
		}


	}

	var d = openSet.poll()
	return [[d.x, d.y]];

}

function clampBetweenZeroAndOne(x) {
	return Math.max(Math.min(x, 1), 0)
}

// this was taken from fixify
function fixupRelaxed(dimension, x, y) {
	height = parseInt(dimension.getHeightAt(x, y) - 0.5)
	left = parseInt(dimension.getHeightAt(x - 1, y) - 0.5)
	right = parseInt(dimension.getHeightAt(x + 1, y) - 0.5)
	top = parseInt(dimension.getHeightAt(x, y - 1) - 0.5)
	bottom = parseInt(dimension.getHeightAt(x, y + 1) - 0.5)

	if (height > left &&
		height > right &&
		height > top &&
		height > bottom) {
		count = count + 1;
		//set the block to the average of the four blocks. (average to make sure snow layers look fine.)
		var sum = dimension.getHeightAt(x + 1, y) + dimension.getHeightAt(x - 1, y) + dimension.getHeightAt(x, y + 1) + dimension.getHeightAt(x, y - 1);
		var average = sum / 4;
		dimension.setHeightAt(x, y, Math.max(average, left + 0.5, right + 0.5, top + 0.5, bottom + 0.5));
	}
	else if (height < left &&
		height < right &&
		height < top &&
		height < bottom) {
		count = count + 1;
		//set the block to the average of the four blocks. (average to make sure snow layers look fine.)
		var sum = dimension.getHeightAt(x + 1, y) + dimension.getHeightAt(x - 1, y) + dimension.getHeightAt(x, y + 1) + dimension.getHeightAt(x, y - 1);
		var average = sum / 4;
		dimension.setHeightAt(x, y, Math.min(average, left + 0.5, right + 0.5, top + 0.5, bottom + 0.5));
	}
}



//################################################################# randomness #################################################################
//source: https://github.com/josephg/noisejs
function initRandom(global) {
	var module = global.noise = {};

	function Grad(x, y, z) {
		this.x = x; this.y = y; this.z = z;
	}

	Grad.prototype.dot2 = function (x, y) {
		return this.x * x + this.y * y;
	};

	Grad.prototype.dot3 = function (x, y, z) {
		return this.x * x + this.y * y + this.z * z;
	};

	var grad3 = [new Grad(1, 1, 0), new Grad(-1, 1, 0), new Grad(1, -1, 0), new Grad(-1, -1, 0),
	new Grad(1, 0, 1), new Grad(-1, 0, 1), new Grad(1, 0, -1), new Grad(-1, 0, -1),
	new Grad(0, 1, 1), new Grad(0, -1, 1), new Grad(0, 1, -1), new Grad(0, -1, -1)];

	var p = [151, 160, 137, 91, 90, 15,
		131, 13, 201, 95, 96, 53, 194, 233, 7, 225, 140, 36, 103, 30, 69, 142, 8, 99, 37, 240, 21, 10, 23,
		190, 6, 148, 247, 120, 234, 75, 0, 26, 197, 62, 94, 252, 219, 203, 117, 35, 11, 32, 57, 177, 33,
		88, 237, 149, 56, 87, 174, 20, 125, 136, 171, 168, 68, 175, 74, 165, 71, 134, 139, 48, 27, 166,
		77, 146, 158, 231, 83, 111, 229, 122, 60, 211, 133, 230, 220, 105, 92, 41, 55, 46, 245, 40, 244,
		102, 143, 54, 65, 25, 63, 161, 1, 216, 80, 73, 209, 76, 132, 187, 208, 89, 18, 169, 200, 196,
		135, 130, 116, 188, 159, 86, 164, 100, 109, 198, 173, 186, 3, 64, 52, 217, 226, 250, 124, 123,
		5, 202, 38, 147, 118, 126, 255, 82, 85, 212, 207, 206, 59, 227, 47, 16, 58, 17, 182, 189, 28, 42,
		223, 183, 170, 213, 119, 248, 152, 2, 44, 154, 163, 70, 221, 153, 101, 155, 167, 43, 172, 9,
		129, 22, 39, 253, 19, 98, 108, 110, 79, 113, 224, 232, 178, 185, 112, 104, 218, 246, 97, 228,
		251, 34, 242, 193, 238, 210, 144, 12, 191, 179, 162, 241, 81, 51, 145, 235, 249, 14, 239, 107,
		49, 192, 214, 31, 181, 199, 106, 157, 184, 84, 204, 176, 115, 121, 50, 45, 127, 4, 150, 254,
		138, 236, 205, 93, 222, 114, 67, 29, 24, 72, 243, 141, 128, 195, 78, 66, 215, 61, 156, 180];
	// To remove the need for index wrapping, double the permutation table length
	var perm = new Array(512);
	var gradP = new Array(512);

	// This isn't a very good seeding function, but it works ok. It supports 2^16
	// different seed values. Write something better if you need more seeds.
	module.seed = function (seed) {
		if (seed > 0 && seed < 1) {
			// Scale the seed out
			seed *= 65536;
		}

		seed = Math.floor(seed);
		if (seed < 256) {
			seed |= seed << 8;
		}

		for (var i = 0; i < 256; i++) {
			var v;
			if (i & 1) {
				v = p[i] ^ (seed & 255);
			} else {
				v = p[i] ^ ((seed >> 8) & 255);
			}

			perm[i] = perm[i + 256] = v;
			gradP[i] = gradP[i + 256] = grad3[v % 12];
		}
	};

	module.seed(0);

	/*
	for(var i=0; i<256; i++) {
	  perm[i] = perm[i + 256] = p[i];
	  gradP[i] = gradP[i + 256] = grad3[perm[i] % 12];
	}*/

	// Skewing and unskewing factors for 2, 3, and 4 dimensions
	var F2 = 0.5 * (Math.sqrt(3) - 1);
	var G2 = (3 - Math.sqrt(3)) / 6;

	var F3 = 1 / 3;
	var G3 = 1 / 6;

	// 2D simplex noise
	module.simplex2 = function (xin, yin) {
		var n0, n1, n2; // Noise contributions from the three corners
		// Skew the input space to determine which simplex cell we're in
		var s = (xin + yin) * F2; // Hairy factor for 2D
		var i = Math.floor(xin + s);
		var j = Math.floor(yin + s);
		var t = (i + j) * G2;
		var x0 = xin - i + t; // The x,y distances from the cell origin, unskewed.
		var y0 = yin - j + t;
		// For the 2D case, the simplex shape is an equilateral triangle.
		// Determine which simplex we are in.
		var i1, j1; // Offsets for second (middle) corner of simplex in (i,j) coords
		if (x0 > y0) { // lower triangle, XY order: (0,0)->(1,0)->(1,1)
			i1 = 1; j1 = 0;
		} else {    // upper triangle, YX order: (0,0)->(0,1)->(1,1)
			i1 = 0; j1 = 1;
		}
		// A step of (1,0) in (i,j) means a step of (1-c,-c) in (x,y), and
		// a step of (0,1) in (i,j) means a step of (-c,1-c) in (x,y), where
		// c = (3-sqrt(3))/6
		var x1 = x0 - i1 + G2; // Offsets for middle corner in (x,y) unskewed coords
		var y1 = y0 - j1 + G2;
		var x2 = x0 - 1 + 2 * G2; // Offsets for last corner in (x,y) unskewed coords
		var y2 = y0 - 1 + 2 * G2;
		// Work out the hashed gradient indices of the three simplex corners
		i &= 255;
		j &= 255;
		var gi0 = gradP[i + perm[j]];
		var gi1 = gradP[i + i1 + perm[j + j1]];
		var gi2 = gradP[i + 1 + perm[j + 1]];
		// Calculate the contribution from the three corners
		var t0 = 0.5 - x0 * x0 - y0 * y0;
		if (t0 < 0) {
			n0 = 0;
		} else {
			t0 *= t0;
			n0 = t0 * t0 * gi0.dot2(x0, y0);  // (x,y) of grad3 used for 2D gradient
		}
		var t1 = 0.5 - x1 * x1 - y1 * y1;
		if (t1 < 0) {
			n1 = 0;
		} else {
			t1 *= t1;
			n1 = t1 * t1 * gi1.dot2(x1, y1);
		}
		var t2 = 0.5 - x2 * x2 - y2 * y2;
		if (t2 < 0) {
			n2 = 0;
		} else {
			t2 *= t2;
			n2 = t2 * t2 * gi2.dot2(x2, y2);
		}
		// Add contributions from each corner to get the final noise value.
		// The result is scaled to return values in the interval [-1,1].
		return 70 * (n0 + n1 + n2);
	};

	// 3D simplex noise
	module.simplex3 = function (xin, yin, zin) {
		var n0, n1, n2, n3; // Noise contributions from the four corners

		// Skew the input space to determine which simplex cell we're in
		var s = (xin + yin + zin) * F3; // Hairy factor for 2D
		var i = Math.floor(xin + s);
		var j = Math.floor(yin + s);
		var k = Math.floor(zin + s);

		var t = (i + j + k) * G3;
		var x0 = xin - i + t; // The x,y distances from the cell origin, unskewed.
		var y0 = yin - j + t;
		var z0 = zin - k + t;

		// For the 3D case, the simplex shape is a slightly irregular tetrahedron.
		// Determine which simplex we are in.
		var i1, j1, k1; // Offsets for second corner of simplex in (i,j,k) coords
		var i2, j2, k2; // Offsets for third corner of simplex in (i,j,k) coords
		if (x0 >= y0) {
			if (y0 >= z0) { i1 = 1; j1 = 0; k1 = 0; i2 = 1; j2 = 1; k2 = 0; }
			else if (x0 >= z0) { i1 = 1; j1 = 0; k1 = 0; i2 = 1; j2 = 0; k2 = 1; }
			else { i1 = 0; j1 = 0; k1 = 1; i2 = 1; j2 = 0; k2 = 1; }
		} else {
			if (y0 < z0) { i1 = 0; j1 = 0; k1 = 1; i2 = 0; j2 = 1; k2 = 1; }
			else if (x0 < z0) { i1 = 0; j1 = 1; k1 = 0; i2 = 0; j2 = 1; k2 = 1; }
			else { i1 = 0; j1 = 1; k1 = 0; i2 = 1; j2 = 1; k2 = 0; }
		}
		// A step of (1,0,0) in (i,j,k) means a step of (1-c,-c,-c) in (x,y,z),
		// a step of (0,1,0) in (i,j,k) means a step of (-c,1-c,-c) in (x,y,z), and
		// a step of (0,0,1) in (i,j,k) means a step of (-c,-c,1-c) in (x,y,z), where
		// c = 1/6.
		var x1 = x0 - i1 + G3; // Offsets for second corner
		var y1 = y0 - j1 + G3;
		var z1 = z0 - k1 + G3;

		var x2 = x0 - i2 + 2 * G3; // Offsets for third corner
		var y2 = y0 - j2 + 2 * G3;
		var z2 = z0 - k2 + 2 * G3;

		var x3 = x0 - 1 + 3 * G3; // Offsets for fourth corner
		var y3 = y0 - 1 + 3 * G3;
		var z3 = z0 - 1 + 3 * G3;

		// Work out the hashed gradient indices of the four simplex corners
		i &= 255;
		j &= 255;
		k &= 255;
		var gi0 = gradP[i + perm[j + perm[k]]];
		var gi1 = gradP[i + i1 + perm[j + j1 + perm[k + k1]]];
		var gi2 = gradP[i + i2 + perm[j + j2 + perm[k + k2]]];
		var gi3 = gradP[i + 1 + perm[j + 1 + perm[k + 1]]];

		// Calculate the contribution from the four corners
		var t0 = 0.6 - x0 * x0 - y0 * y0 - z0 * z0;
		if (t0 < 0) {
			n0 = 0;
		} else {
			t0 *= t0;
			n0 = t0 * t0 * gi0.dot3(x0, y0, z0);  // (x,y) of grad3 used for 2D gradient
		}
		var t1 = 0.6 - x1 * x1 - y1 * y1 - z1 * z1;
		if (t1 < 0) {
			n1 = 0;
		} else {
			t1 *= t1;
			n1 = t1 * t1 * gi1.dot3(x1, y1, z1);
		}
		var t2 = 0.6 - x2 * x2 - y2 * y2 - z2 * z2;
		if (t2 < 0) {
			n2 = 0;
		} else {
			t2 *= t2;
			n2 = t2 * t2 * gi2.dot3(x2, y2, z2);
		}
		var t3 = 0.6 - x3 * x3 - y3 * y3 - z3 * z3;
		if (t3 < 0) {
			n3 = 0;
		} else {
			t3 *= t3;
			n3 = t3 * t3 * gi3.dot3(x3, y3, z3);
		}
		// Add contributions from each corner to get the final noise value.
		// The result is scaled to return values in the interval [-1,1].
		return 32 * (n0 + n1 + n2 + n3);

	};

	// ##### Perlin noise stuff

	function fade(t) {
		return t * t * t * (t * (t * 6 - 15) + 10);
	}

	function lerp(a, b, t) {
		return (1 - t) * a + t * b;
	}

	// 2D Perlin Noise
	module.perlin2 = function (x, y) {
		// Find unit grid cell containing point
		var X = Math.floor(x), Y = Math.floor(y);
		// Get relative xy coordinates of point within that cell
		x = x - X; y = y - Y;
		// Wrap the integer cells at 255 (smaller integer period can be introduced here)
		X = X & 255; Y = Y & 255;

		// Calculate noise contributions from each of the four corners
		var n00 = gradP[X + perm[Y]].dot2(x, y);
		var n01 = gradP[X + perm[Y + 1]].dot2(x, y - 1);
		var n10 = gradP[X + 1 + perm[Y]].dot2(x - 1, y);
		var n11 = gradP[X + 1 + perm[Y + 1]].dot2(x - 1, y - 1);

		// Compute the fade curve value for x
		var u = fade(x);

		// Interpolate the four results
		return lerp(
			lerp(n00, n10, u),
			lerp(n01, n11, u),
			fade(y));
	};

	// 3D Perlin Noise
	module.perlin3 = function (x, y, z) {
		// Find unit grid cell containing point
		var X = Math.floor(x), Y = Math.floor(y), Z = Math.floor(z);
		// Get relative xyz coordinates of point within that cell
		x = x - X; y = y - Y; z = z - Z;
		// Wrap the integer cells at 255 (smaller integer period can be introduced here)
		X = X & 255; Y = Y & 255; Z = Z & 255;

		// Calculate noise contributions from each of the eight corners
		var n000 = gradP[X + perm[Y + perm[Z]]].dot3(x, y, z);
		var n001 = gradP[X + perm[Y + perm[Z + 1]]].dot3(x, y, z - 1);
		var n010 = gradP[X + perm[Y + 1 + perm[Z]]].dot3(x, y - 1, z);
		var n011 = gradP[X + perm[Y + 1 + perm[Z + 1]]].dot3(x, y - 1, z - 1);
		var n100 = gradP[X + 1 + perm[Y + perm[Z]]].dot3(x - 1, y, z);
		var n101 = gradP[X + 1 + perm[Y + perm[Z + 1]]].dot3(x - 1, y, z - 1);
		var n110 = gradP[X + 1 + perm[Y + 1 + perm[Z]]].dot3(x - 1, y - 1, z);
		var n111 = gradP[X + 1 + perm[Y + 1 + perm[Z + 1]]].dot3(x - 1, y - 1, z - 1);

		// Compute the fade curve value for x, y, z
		var u = fade(x);
		var v = fade(y);
		var w = fade(z);

		// Interpolate
		return lerp(
			lerp(
				lerp(n000, n100, u),
				lerp(n001, n101, u), w),
			lerp(
				lerp(n010, n110, u),
				lerp(n011, n111, u), w),
			v);
	};

}

//###################### \/ functions for saving the last entered values \/ ########################################
function createAndWriteFile(filePath, content) {
    var path = Paths.get(filePath);
    
    try {
        // Create the file if it doesn't exist, truncate it if it does
        var writer = fs.newBufferedWriter(path, [StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING]);
        
        // Write the content to the file
        writer.write(content);
        writer.newLine();
        
        // Close the writer
        writer.close();
        
        print("File updated successfully:", filePath);
    } catch (e) {
        print("Error updating file:", e);
    }
}



function readFile(filePath) {
    var path = Paths.get(filePath);
    if (!fs.exists(path)) {
        print("ERROR:", filePath, "does not exist.")
        return null;
    }
    var content = new java.lang.String(fs.readAllBytes(path), StandardCharsets.UTF_8).toString();
    return content;
}

function replaceParamValue(inputString, patternsAndValues) {
    var lines = inputString.split('\n'); // Split inputString into lines
    var updatedLines = [];

    lines.forEach(function(line) {
        var lineUpdated = false;
        patternsAndValues.forEach(function(tuple) {
            var pattern = tuple[0];
            var newValue = tuple[1];
            if (newValue == undefined){
                newValue = "";
            }

            if (line.startsWith(pattern)) {
                var newLine = pattern + newValue;
                updatedLines.push(newLine);
                lineUpdated = true;
            }
        });

        if (!lineUpdated) {
            updatedLines.push(line); // Push lines that don't match any pattern unchanged
        }
    });

    // Join the updated lines back into a single string
    var updatedString = updatedLines.join('\n');
    return updatedString;
}
//###################### /\ functions for saving the last entered values /\ ########################################


















