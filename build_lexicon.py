"""从 assets/ecdict.csv 生成 lexicon_data.py（词频表 + 专名表）。

为什么要生成而不是运行时读 ecdict.csv:
  - 词频表只需要 word/bnc/frq 三列，但 csv 整表解析要 2.0 秒；
    压成 zlib+base64 常量后，运行期解压只要几十毫秒。
  - 不额外产生数据文件 —— 数据直接嵌进 .py（PYZ 内），
    打包后 _internal 目录结构不变，只需替换 exe 即可发布。

两张表:
  1. FREQ   词频表 {word: rank}，rank = min(bnc, frq)（榜单排名，越小越常用）。
            只保留有排名的词（约 5 万）。用于 OCR 候选仲裁:
            "有排名者优先、排名小者优先"，根治 th1s->thrs 这类同形歧义。
  2. NAMES  专名表 {word}，用于"句中首字母误大写还原"的护栏，
            避免 with Mark 被误改成 with mark。
            组成 = 内置常见英文专名 + ecdict 里标注 "(Xxx)人名" 的词形。

用法: venv python build_lexicon.py
"""
import base64
import csv
import re
import sys
import zlib

sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 9)

CSV_PATH = "assets/ecdict.csv"
OUT_PATH = "lexicon_data.py"

# ---------------------------------------------------------------- 专名人工表
# 只收"明确是专名"的词；刻意**不含** will/may/can/should/would/must 等
# 情态助动词——它们作为普通词的使用频率远高于作为人名，保护起来会
# 让 "You May go" / "the Will" 这类误大写永远修不回来。
_PROPER_MANUAL = """
Aaron Adam Alan Albert Alex Alexander Andrew Andy Anthony Arthur
Austin Barry Ben Benjamin Bernard Bill Billy Bob Bobby Brad Brandon
Brendan Brian Bruce Bryan Caleb Carl Carlos Charles Charlie Chris
Christian Christopher Clark Cody Colin Connor Craig Curtis Dale Dan
Daniel Danny Darren Dave David Dean Dennis Derek Donald Douglas Duane
Dustin Dylan Ed Eddie Edgar Edward Edwin Elijah Eric Ernest Ethan
Eugene Evan Felix Fernando Floyd Francis Frank Fred Gabriel Gary
Gene George Gerald Gilbert Glen Gordon Greg Gregory Guy Harold Harry
Hank Harvey Hector Henry Herbert Howard Hugh Ian Isaac Ivan Jack
Jacob Jake James Jamie Jared Jason Javier Jay Jeff Jeffrey Jeremy
Jerry Jesse Jesus Jim Jimmy Joe Joel John Johnny Jon Jonathan Jordan
Jose Joseph Josh Joshua Juan Julian Justin Karl Keith Ken Kenneth
Kevin Kurt Kyle Lance Larry Lee Leo Leon Leonard Leroy Lewis Liam
Logan Louis Lucas Luke Luis Manuel Marc Marcus Mario Mark Martin
Marty Mason Matt Matthew Maurice Max Maxwell Michael Miguel Mike
Miles Mitchell Nathan Neal Neil Nelson Nicholas Nick Nicolas Noah
Norman Oliver Oscar Owen Patrick Paul Pedro Perry Pete Peter Phil
Philip Phillip Ralph Ramon Randall Randy Ray Raymond Reggie Ricardo
Richard Rick Ricky Rob Robert Roberto Rodney Roger Roland Ron Ronald
Ross Roy Russell Ryan Salvador Sam Sammy Samuel Scott Sean Sergio
Seth Shane Shawn Sidney Simon Spencer Stanley Stephen Steve Steven
Stewart Stuart Ted Terrance Terry Theo Thomas Tim Timothy Toby Todd
Tom Tommy Tony Travis Trevor Troy Tyler Tyrone Victor Vince Vincent
Virgil Wade Wallace Walter Warren Wayne Wendell Wesley William Willie
Zach Zachary
Abby Abigail Ada Adeline Alice Alicia Alison Amanda Amber Amy Andrea
Angela Angelina Ann Anna Anne Annie Ashley Audrey Barbara Beatrice
Becky Bella Belle Betty Beverly Blanche Bonnie Brenda Brittany
Caroline Carol Carrie Cassandra Catherine Cathy Cecilia Charlotte
Chelsea Chloe Christina Christine Cindy Claire Clara Claudia Colleen
Connie Constance Crystal Daisy Dana Danielle Daphne Darlene Dawn
Deborah Debra Delia Denise Diana Diane Dolores Donna Dora Doris
Dorothy Edith Eileen Elaine Eleanor Elena Elizabeth Ella Ellen
Elsie Emily Emma Erica Erin Estelle Ethel Eva Evelyn Fannie Fiona
Florence Frances Gabrielle Gail Georgia Geraldine Gertrude Gina
Gladys Gloria Grace Gwen Hannah Harriet Hazel Heather Helen Holly
Hope Ida Imogen Irene Iris Isabel Isabella Ivy Jackie Jacqueline
Jane Janice Janis Jasmine Jean Jeanette Jeanne Jennifer Jenny
Jessica Jill Joan Joanna Joanne Jocelyn Jodie Josephine Joy Joyce
Juanita Judith Judy Julia Julianne Julie Juliet June Karen Kate
Katherine Kathleen Kathy Katie Katrina Kay Kayla Kelly Kimberly
Kristen Kristin Laura Lauren Leah Lena Leslie Lila Lily Linda Lisa
Liz Lizzy Lois Lola Lorraine Louise Lucia Lucille Lucy Lydia
Madison Mae Maggie Maisie Mandy Marcia Margaret Maria Marian
Marilyn Marion Marjorie Martha Mary Maude Maureen Mavis Maxine
Megan Melanie Melinda Melissa Mia Michelle Mildred Millie Miranda
Molly Monica Morgan Muriel Myra Myrtle Nadine Nancy Naomi Natalie
Natasha Nellie Nettie Nina Nora Norma Olivia Opal Pamela Patricia
Patsy Patty Paula Paulette Pearl Peggy Penelope Penny Phoebe Phyllis
Rachel Ramona Rebecca Regina Renee Rhoda Rita Roberta Robin Rosa
Rosalie Rose Rosemarie Rosemary Ruth Sabrina Sadie Sally Samantha
Sandra Sandy Sara Sarah Selena Sharon Sheila Shelley Shirley Silva
Sophia Sophie Stella Stephanie Sue Susan Susie Suzanne Sylvia
Tabitha Tamara Tammy Tanya Tara Teresa Tess Tessa Thelma Theresa
Tiffany Tina Toni Tonya Tracy Trisha Valerie Vanessa Velma Vera
Verna Veronica Vicki Vickie Victoria Viola Violet Virginia Vivian
Wanda Wendy Whitney Wilma Yvonne Zoe
Abbott Adams Aldridge Baker Baldwin Barnes Barrett Barton Bass Bates
Baxter Beck Becker Bell Bennett Benson Bentley Bishop Black Blair
Blake Boone Bowen Boyd Boyle Bradley Brady Branch Brewer Bridges
Briggs Brock Brooks Brown Bruce Bryant Burke Burns Burton Bush
Butler Byrd Cain Caldwell Cameron Campbell Cannon Carlson Carpenter
Carr Carroll Carter Casey Chambers Chandler Chapman Chase Christensen
Clark Clarke Clayton Cobb Cochran Cole Coleman Collier Collins Conner
Conrad Conway Cook Cooke Cooley Cooper Copeland Corbett Cortez Cox
Craft Crane Crawford Crosby Cross Cummings Cunningham Curry Curtis
Dalton Daniels Davenport David Dawson Day Dean Decker Delgado Dennis
Diaz Dixon Dodson Dominguez Donaldson Donnelly Donovan Dorsey Dotson
Douglas Doyle Drake Dudley Duffy Duke Duncan Dunlap Dunn Eaton Edwards
Elliott Ellis Emerson England Erickson Estrada Evans Farmer Farrell
Faulkner Ferguson Fields Finch Fischer Fisher Fitzgerald Fitzpatrick
Fleming Fletcher Flowers Floyd Flynn Foley Forbes Ford Foreman
Foster Fowler Fox Francis Franklin Frazier Freeman French Frost
Fry Fuller Gaines Gallagher Gamble Gardner Garner Garrett Garrison
Gibbs Gibson Gilbert Giles Gill Gillespie Gilmore Glover Goff Golden
Gomez Goodman Goodwin Gordon Gould Graham Grant Graves Gray Green
Greene Gregory Griffin Griffith Grimes Gross Guerrero Guthrie Hahn
Hale Haley Hall Hamilton Hammond Hampton Hancock Haney Hanson Harding
Hardy Harmon Harper Harrell Harrington Harris Harrison Hart Hartman
Harvey Hatfield Hawkins Hayden Haynes Hays Heath Henderson Hendricks
Henry Hensley Henson Herman Hernandez Herring Hess Hewitt Hicks
Higgins Hill Hines Hinton Hobbs Hodges Hoffman Hogan Holden Holland
Holloway Holmes Holt Hood Hooper Hoover Hopkins Horn Horne Horton
House Houston Howard Howe Howell Hubbard Huber Hudson Huff Hughes
Hull Humphrey Hunt Hunter Hurley Hurst Hutchinson Hyde Ingram Irwin
Jackson Jacobs Jacobson James Jarvis Jefferson Jenkins Jennings
Jensen Jimenez Johns Johnson Johnston Jordan Joseph Joyce Joyner
Justice Kane Kaufman Kay Keating Keller Kelley Kelly Kemp Kennedy
Kent Kerr Kessler Key Kidd Kimball Kinney Kirby Kirk Klein Kline
Knapp Knight Knowles Knox Koch Kramer Lamb Lambert Lancaster Landry
Lane Lang Langley Larsen Larson Lawrence Lawson Leach Leblanc Lee
Leonard Lester Levine Levy Lewis Lindsey Little Livingston Lloyd
Logan Long Lopez Love Lowe Lucas Luna Lyons Macdonald Macias Mack
Madden Maddox Maldonado Malone Mann Manning Marks Marquez Marsh
Marshall Martin Martinez Mason Mathews Mathis Matthews Maxwell May
Mayer Maynard Mayo Mays Mcbride Mccall Mccarthy Mccarty Mcclain
Mcconnell Mccormick Mccoy Mccullough Mcdaniel Mcdowell Mcfarland
Mcgee Mcguire Mcintosh Mcintyre Mckay Mckee Mckenzie Mckinney Mclaughlin
Mclean Mcmahon Mcmillan Mcneil Mcpherson Meadows Medina Mejia Melton
Mendez Mendoza Mercer Merritt Meyers Michael Middleton Miles Miller
Mills Miranda Mitchell Monroe Montgomery Moody Moon Mooney Moore
Morales Moran Moreno Morgan Morin Morris Morrison Morrow Morton Moss
Moyer Mueller Mullen Mullins Munoz Murphy Murray Myers Nash Navarro
Neal Nelson Newell Newman Newton Nichols Nicholson Nielsen Nixon
Noble Nolan Norman Norris Norton Novak Nunez Obrien Ochoa Odell
Odom Ogle Oliver Olsen Olson Oneal Orozco Orr Ortega Ortiz Osborn
Osborne Owen Owens Pace Padilla Page Palmer Park Parker Parks
Parrish Parsons Patrick Patterson Patton Payne Pearson Peck Pena
Pennington Perez Perkins Perry Peters Petersen Peterson Petty Phelps
Phillips Pickett Pierce Pittman Pitts Pollard Poole Pope Porter
Potter Potts Powell Powers Pratt Preston Price Prince Proctor Pruitt
Quinn Ramsey Randall Randolph Rasmussen Ratliff Ray Raymond Reed
Reese Reeves Reid Reilly Reyes Reynolds Rhodes Rice Rich Richardson
Richmond Riddle Riggs Riley Rios Rivas Rivera Rivers Roach Robbins
Roberson Roberts Robertson Robinson Robles Rocha Rodgers Rodriguez
Rogers Rojas Rollins Roman Romero Rosa Rosales Rosario Rose Ross
Roth Rowe Rowland Roy Rubio Ruiz Rush Russell Russo Ryan Salazar
Salinas Sampson Sanchez Sanders Sandoval Sanford Santana Santiago
Santos Sargent Saunders Savage Sawyer Schmidt Schmitt Schneider
Schroeder Schultz Schwartz Scott Sears Sellers Serrano Sessions
Sexton Shaffer Shannon Sharp Shaw Shea Shepard Shepherd Sheppard
Sherman Shields Short Silva Simmons Simon Simpson Sims Sinclair
Singleton Skinner Slater Sloan Small Smith Snow Snyder Solis Solomon
Sosa Soto Sparks Spears Spencer Stafford Stanley Stanton Stark
Steele Stein Stephens Stephenson Stevens Stevenson Stewart Stokes
Stone Stout Strickland Strong Stuart Suarez Sullivan Summers Sweeney
Swift Tanner Tate Taylor Terrell Terry Thomas Thompson Thornton
Tillman Todd Torres Townsend Tran Travis Trevino Trujillo Tucker
Turner Tyler Underwood Valdez Valencia Valentine Valenzuela Vance
Vang Vargas Vasquez Vaughan Vaughn Vega Velasquez Velazquez Velez
Villarreal Vincent Wade Wagner Walker Wall Wallace Waller Walls
Walsh Walter Walters Walton Ward Ware Warner Warren Washington
Waters Watkins Watson Watts Weaver Webb Weber Webster Weeks Weiner
Weiss Welch Wells West Wheeler Whitaker White Whitehead Whitfield
Whitney Wiggins Wilcox Wilder Wiley Wilkerson Wilkins Wilkinson
Williams Williamson Willis Wilson Winters Wise Wolf Wolfe Wong Wood
Woodard Woods Woodward Wooten Workman Wright Wyatt Yang Yates York
Young Zamora Zimmerman
Paris London Tokyo Berlin Rome Madrid Moscow Vienna Venice Amsterdam
Dublin Lisbon Athens Prague Warsaw Budapest Moscow Cairo Sydney
Melbourne Toronto Vancouver Montreal Chicago Boston Seattle Portland
Denver Dallas Houston Phoenix Austin Atlanta Detroit Miami Nashville
Orlando Memphis Baltimore Philadelphia Pittsburgh Cleveland Cincinnati
Milwaukee Kansas Sacramento Oakland Minneapolis Tampa Tucson Omaha
Anchorage Honolulu York Kent Essex Sussex Surrey Dorset Devon Cornwall
Scotland England Ireland Wales Britain Europe Asia Africa America
Canada Mexico Brazil Argentina Peru Chile Egypt India Japan China
Korea Thailand Vietnam Russia Poland Norway Sweden Denmark Finland
Iceland Greece Turkey Israel Iran Iraq Cuba Jamaica Bahamas Bermuda
Alaska Hawaii Texas California Florida Nevada Arizona Oregon Montana
Kansas Virginia Georgia Maryland Delaware Vermont Wyoming Colorado
Oxford Cambridge Harvard Yale Princeton Stanford Broadway Hollywood
Manhattan Brooklyn Queens Bronx Chicago Detroit Salem Springfield
Riverside Fairview Franklin Clinton Georgetown Lexington Madison
January February March April June July August September October
November December Monday Tuesday Wednesday Thursday Friday Saturday
Sunday Christmas Halloween Thanksgiving Easter Newyear Santa Satan
God Lord Jesus Christ Buddha Allah Yahweh Jehovah
Google Apple Windows Linux Facebook Twitter Amazon PlayStation Xbox
Nintendo Disney Marvel Batman Superman Sherlock Watson Dracula
""".split()


def build():
    freq = {}
    extracted = set()
    pat_paren = re.compile(r"\(([A-Z][A-Za-z'\-]{1,18})\)人名")

    n = 0
    with open(CSV_PATH, encoding="utf-8", errors="ignore", newline="") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            n += 1
            if len(row) < 10:
                continue
            w = (row[0] or "").strip().lower()
            if not w or not w.isascii():
                continue
            try:
                bnc_i = int(row[8] or 0)
            except ValueError:
                bnc_i = 0
            try:
                frq_i = int(row[9] or 0)
            except ValueError:
                frq_i = 0
            ranks = [x for x in (bnc_i, frq_i) if x > 0]
            if ranks:
                freq[w] = min(ranks)
            t = row[3] or ""
            if "人名" in t:
                for m in pat_paren.findall(t):
                    if 2 <= len(m) <= 20:
                        extracted.add(m.lower())

    # 专名表 = 人工表 + ecdict 提取；剔除情态助动词（保护它们会挡住必要的修复）
    modal = {
        "will", "would", "can", "could", "shall", "should", "may", "might",
        "must", "do", "does", "did", "have", "has", "had", "be", "is", "are",
        "was", "were", "am", "been", "being", "ought", "need", "dare",
    }
    names = set(x.lower() for x in _PROPER_MANUAL)
    names |= extracted
    names -= modal
    names = {x for x in names if 2 <= len(x) <= 20 and x.isascii()}

    print(f"ecdict 行数: {n}")
    print(f"词频表(有排名): {len(freq)}")
    print(f"专名表: {len(names)}  (人工 {len(set(x.lower() for x in _PROPER_MANUAL))}"
          f" + 提取 {len(extracted)})")

    freq_txt = "\n".join(f"{w}\t{r}" for w, r in sorted(freq.items()))
    names_txt = "\n".join(sorted(names))

    def enc(s):
        b64 = base64.b64encode(zlib.compress(s.encode("utf-8"), 9)).decode("ascii")
        rows = [b64[i:i + 110] for i in range(0, len(b64), 110)]
        return '\n    "' + '"\n    "'.join(rows) + '"'

    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write('"""OCR 词库数据（自动生成，勿手改）。\n\n'
                '生成脚本: build_lexicon.py  —— 数据源 assets/ecdict.csv\n'
                '  FREQ_B64  词频表 word->rank（zlib+base64），约 %d 词\n'
                '  NAMES_B64 专名表（zlib+base64），约 %d 词\n'
                '"""\n\n' % (len(freq), len(names)))
        f.write("FREQ_B64 = (" + enc(freq_txt) + "\n)\n\n")
        f.write("NAMES_B64 = (" + enc(names_txt) + "\n)\n")

    import os
    print(f"已写出 {OUT_PATH}  ({os.path.getsize(OUT_PATH) / 1024:.0f} KB)")


if __name__ == "__main__":
    build()
