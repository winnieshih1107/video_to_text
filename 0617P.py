""" 

print(st)
print(len(st)) #計算有幾個字

"""


orgStr = """國立中興大學（簡稱興大、NCHU）是一所校本部位於台灣臺中市南
區的國立研究型綜合大學，起源自臺灣總督府創辦的農林專門學校，臺北帝國大學
設立後併入為附屬農林專門部，而後於1943年獨立設校並遷址台中。中興大學以農
業科學、農業經濟學、獸醫學、生命科學、轉譯醫學、材料科學、生醫工程、生物
科技、綠色科技等研究領域見長，校友共有7位中央研究院院士，皆為生命科學組。
[7]興大目前共有12個學院與興大附農、興大附中兩所附屬中學。近年與臺中榮民總
醫院、彰化師範大學、中國醫藥大學等機構合作，2022年學士後醫學系設立，為台
灣中部第一所國立大學醫學系。興大醫學院並未設置教學醫院而採用美國哈佛醫學
院模式與鄰近醫院合作培養醫學人才。[8]興大也與臺中市政府合作，簽訂合作意
向書，共同推動數位文化、智慧城市帶動區域發展[9]。
st = set(orgStr)
dt = dict()
for c in st:
    dt[c] = orgStr.count(c)
print(dt)
"""


"""
隨機產生15個1~50 之間的整數，找出其中的第2大數值
不能使用排序函式
"""
"""
import random
random.seed(17)
sample = random.choices(range(1, 51), k=15)
num_max = None
num_2nd = None
for num in sample:
    if num_max == None:
        num_max = num
        continue
    # ---
    if num > num_max:
        num_2nd = num_max
        num_max = num
    else:
        if num_2nd == None:
            num_2nd = num
        # ---
        if num > num_2nd:
            num_2nd = num
# ---
sample.sort(reverse=True)
print(sample)
print(num_2nd)
"""

"""
利用user_list 名單，搭配隨機功能，為每一位user 
產生一個介於(1~100)的成績，再從中找出最高分的user
"""
import random
user_list = ["Aaric", "Abbot" , "Ace"  , "Ackerley", "Adam" , "Adney"     , 
             "Bab"  , "Bamboo", "Ben"  , "Bunny"   , "Betty", "Baha"      , 
             "Cindy", "Candy" , "Cathy", "Cakra"   , "Carin", "Caroline"  ,
             "Deny" , "Dacy"  , "Danna", "Debbi"   , "Devon", "Diza","Dob"]
user_dict = dict()
for user in user_list:
    user_dict[user] = random.randint(1, 10)
# ---
print(user_dict)
user_na = max_scor = None
for k, v in user_dict.items():
    if user_na == None:
        user_na  = [k]
        max_scor = v
        continue
    # ---
    if   v > max_scor:
        user_na  = [k]
        max_scor = v
    elif v == max_scor:
        user_na.append(k)
else:
    print(user_na, " : ", max_scor)
